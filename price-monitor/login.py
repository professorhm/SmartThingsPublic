#!/usr/bin/env python3
"""
Run this once (and whenever your session expires) to authenticate.
A browser window will open — complete the login and 2FA yourself,
then press Enter in this terminal to save the session and close.
"""

import asyncio
import os
from pathlib import Path

from dotenv import load_dotenv
from playwright.async_api import async_playwright

load_dotenv()

ANTH_EMAIL = os.getenv("ANTH_EMAIL", "")
SESSION_FILE = Path(__file__).parent / "session.json"
LOGIN_URL = "https://www.anthropologie.com/login"
FAVORITES_URL = "https://www.anthropologie.com/favorites"


async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False)
        context = await browser.new_context()
        page = await context.new_page()

        await page.goto(LOGIN_URL)

        # Pre-fill email if provided in .env
        if ANTH_EMAIL:
            try:
                await page.fill(
                    'input[type="email"], input[name="email"], input[placeholder*="Email" i]',
                    ANTH_EMAIL,
                )
            except Exception:
                pass

        print("=================================================")
        print("  Browser is open. Complete your login + 2FA.")
        print("  Once you can see your favorites/account page,")
        print("  come back here and press Enter to save session.")
        print("=================================================")
        input()

        # Verify we actually ended up logged in
        await page.goto(FAVORITES_URL, wait_until="networkidle")
        if "login" in page.url.lower() or "sign-in" in page.url.lower():
            print("Doesn't look like login completed — session NOT saved.")
            print("Try again and make sure you're fully signed in before pressing Enter.")
            await browser.close()
            return

        await context.storage_state(path=str(SESSION_FILE))
        await browser.close()

    print(f"Session saved to {SESSION_FILE}")
    print("You can now run price_monitor.py on its schedule.")


if __name__ == "__main__":
    asyncio.run(main())
