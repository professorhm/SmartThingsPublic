#!/usr/bin/env python3
"""
Anthropologie Price Drop Monitor

Checks your favorited items for price drops, adds them to your cart,
and sends you an email asking whether to proceed to checkout.
The purchase is NEVER completed automatically — you always confirm via email.
"""

import asyncio
import json
import os
import re
import smtplib
import sys
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from playwright.async_api import async_playwright, Page

load_dotenv()

ANTH_EMAIL = os.getenv("ANTH_EMAIL", "")
ANTH_PASSWORD = os.getenv("ANTH_PASSWORD", "")
NOTIFY_EMAIL = os.getenv("NOTIFY_EMAIL", "")
SMTP_EMAIL = os.getenv("SMTP_EMAIL", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")   # Gmail App Password, not your account password
# Minimum dollar drop required to trigger alert (avoids noise from $0.01 fluctuations)
PRICE_DROP_THRESHOLD = float(os.getenv("PRICE_DROP_THRESHOLD", "1.00"))
HEADLESS = os.getenv("HEADLESS", "true").lower() != "false"

BASE_DIR = Path(__file__).parent
PRICES_FILE = BASE_DIR / "prices.json"
SESSION_FILE = BASE_DIR / "session.json"   # Saved login state, reused across runs

LOGIN_URL = "https://www.anthropologie.com/login"
FAVORITES_URL = "https://www.anthropologie.com/favorites"
CART_URL = "https://www.anthropologie.com/bag"


# ---------------------------------------------------------------------------
# Price persistence
# ---------------------------------------------------------------------------

def load_prices() -> dict:
    if PRICES_FILE.exists():
        with open(PRICES_FILE) as f:
            return json.load(f)
    return {}


def save_prices(prices: dict):
    with open(PRICES_FILE, "w") as f:
        json.dump(prices, f, indent=2)


# ---------------------------------------------------------------------------
# Email notification — this is the "confirmation" step.
# Items are added to cart; you decide whether to check out.
# ---------------------------------------------------------------------------

def send_notification(drops: list[dict]):
    if not all([SMTP_EMAIL, SMTP_PASSWORD, NOTIFY_EMAIL]):
        # Fall back to console output if email isn't configured
        print("\n====== PRICE DROP ALERT ======")
        for d in drops:
            drop_amt = d["old_price"] - d["new_price"]
            pct = (drop_amt / d["old_price"]) * 100
            print(f"  {d['name']}")
            print(f"    Was: ${d['old_price']:.2f}  →  Now: ${d['new_price']:.2f}  (-${drop_amt:.2f}, {pct:.0f}% off)")
            print(f"    {d['url']}")
        print(f"\nItems added to cart. Review at: {CART_URL}")
        print("==============================\n")
        return

    subject = f"[Anthropologie] {len(drops)} price drop(s) — items added to your bag"

    rows = ""
    for d in drops:
        drop_amt = d["old_price"] - d["new_price"]
        pct = (drop_amt / d["old_price"]) * 100
        rows += f"""
        <tr>
          <td style="padding:8px"><a href="{d['url']}" style="color:#333">{d['name']}</a></td>
          <td style="padding:8px;text-decoration:line-through;color:#999">${d['old_price']:.2f}</td>
          <td style="padding:8px;color:#c00;font-weight:bold">${d['new_price']:.2f}</td>
          <td style="padding:8px;color:#060">-${drop_amt:.2f} ({pct:.0f}% off)</td>
        </tr>"""

    body = f"""
    <html>
    <body style="font-family:sans-serif;color:#333;max-width:600px;margin:auto">
      <h2 style="border-bottom:2px solid #c00;padding-bottom:8px">
        Price Drop Alert &mdash; Anthropologie
      </h2>
      <p>The items below dropped in price and have been added to your bag.
         <strong>No purchase has been made</strong> &mdash; review your bag and
         check out only if you want them.</p>
      <table border="0" cellpadding="0" cellspacing="0"
             style="width:100%;border-collapse:collapse;border:1px solid #ddd">
        <tr style="background:#f5f5f5">
          <th style="padding:8px;text-align:left">Item</th>
          <th style="padding:8px;text-align:left">Was</th>
          <th style="padding:8px;text-align:left">Now</th>
          <th style="padding:8px;text-align:left">Savings</th>
        </tr>
        {rows}
      </table>
      <p style="margin-top:24px">
        <a href="{CART_URL}"
           style="background:#c00;color:#fff;padding:12px 24px;
                  text-decoration:none;border-radius:4px;display:inline-block">
          Review Bag &amp; Check Out &rarr;
        </a>
      </p>
      <p style="color:#999;font-size:12px">
        To skip these items, simply ignore this email.
        They will remain in your bag until you remove them or the session expires.
      </p>
    </body>
    </html>"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = SMTP_EMAIL
    msg["To"] = NOTIFY_EMAIL
    msg.attach(MIMEText(body, "html"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(SMTP_EMAIL, SMTP_PASSWORD)
            server.sendmail(SMTP_EMAIL, NOTIFY_EMAIL, msg.as_string())
        print(f"Notification email sent to {NOTIFY_EMAIL}")
    except Exception as e:
        print(f"Failed to send email: {e}")
        print(f"Check SMTP_EMAIL / SMTP_PASSWORD in your .env file.")


# ---------------------------------------------------------------------------
# Browser helpers
# ---------------------------------------------------------------------------

def parse_price(text: str) -> Optional[float]:
    match = re.search(r"\$?([\d,]+\.?\d*)", text.replace(",", ""))
    return float(match.group(1)) if match else None


async def ensure_logged_in(page: Page):
    await page.goto(FAVORITES_URL, wait_until="networkidle")
    if "login" not in page.url.lower() and "sign-in" not in page.url.lower():
        return  # Already logged in via saved session

    print("Session expired or first run — logging in...")
    await page.goto(LOGIN_URL, wait_until="networkidle")

    await page.fill(
        'input[type="email"], input[name="email"], input[placeholder*="Email" i]',
        ANTH_EMAIL,
    )
    await page.fill('input[type="password"], input[name="password"]', ANTH_PASSWORD)
    await page.click(
        'button[type="submit"], button:has-text("Sign In"), button:has-text("Log In")'
    )
    await page.wait_for_load_state("networkidle")

    if "login" in page.url.lower() or "sign-in" in page.url.lower():
        raise RuntimeError(
            "Login failed. Double-check ANTH_EMAIL and ANTH_PASSWORD in your .env file."
        )
    print("Logged in successfully.")


async def scrape_favorites(page: Page) -> list[dict]:
    print(f"Loading favorites...")
    await page.goto(FAVORITES_URL, wait_until="networkidle")

    # Scroll to trigger lazy-loaded product cards
    for _ in range(6):
        await page.keyboard.press("End")
        await asyncio.sleep(1.2)

    items = []
    # Anthropologie renders product cards with several possible class patterns
    cards = await page.query_selector_all(
        '[data-testid="product-card"], '
        '[class*="ProductCard"], '
        '[class*="product-card"], '
        'article[class*="product"]'
    )
    if not cards:
        # Broader fallback — any article/li containing a price
        cards = await page.query_selector_all("article, li")

    for card in cards:
        try:
            name_el = await card.query_selector(
                '[data-testid="product-name"], [class*="ProductName"], '
                '[class*="product-name"], h2, h3'
            )
            if not name_el:
                continue
            name = (await name_el.inner_text()).strip()

            link_el = await card.query_selector("a[href]")
            href = (await link_el.get_attribute("href")) if link_el else ""
            if not href:
                continue
            url = (
                f"https://www.anthropologie.com{href}"
                if href.startswith("/")
                else href
            )

            # Prefer explicit sale-price element; fall back to any price element
            sale_el = await card.query_selector(
                '[data-testid="sale-price"], [class*="SalePrice"], [class*="sale-price"]'
            )
            price_el = await card.query_selector(
                '[data-testid="price"], [class*="Price"]:not([class*="Sale"]):not([class*="sale"])'
            )

            sale_text = (await sale_el.inner_text()).strip() if sale_el else ""
            price_text = (await price_el.inner_text()).strip() if price_el else ""

            current_price = parse_price(sale_text) or parse_price(price_text)
            if current_price is None or not name:
                continue

            items.append({"name": name, "url": url, "price": current_price})
        except Exception as e:
            print(f"  Warning: skipped a card — {e}")

    print(f"Found {len(items)} favorited item(s).")
    return items


async def add_to_cart(page: Page, item: dict) -> bool:
    try:
        await page.goto(item["url"], wait_until="networkidle")

        # Select the first available size if size options are present
        # (Anthropologie requires a size selection before "Add to Bag" activates)
        size_btns = await page.query_selector_all(
            '[data-testid="size-button"]:not([disabled]):not([aria-disabled="true"]), '
            '[class*="SizeButton"]:not([disabled]):not([aria-disabled="true"])'
        )
        if size_btns:
            await size_btns[0].click()
            await asyncio.sleep(0.8)

        add_btn = await page.query_selector(
            'button:has-text("Add to Bag"), '
            'button:has-text("Add to Cart"), '
            '[data-testid="add-to-bag"], '
            '[data-testid="add-to-cart"]'
        )
        if not add_btn:
            print(f"  No 'Add to Bag' button found for: {item['name']}")
            return False

        is_disabled = await add_btn.get_attribute("disabled")
        if is_disabled is not None:
            print(f"  'Add to Bag' is disabled (item may be sold out): {item['name']}")
            return False

        await add_btn.click()
        await asyncio.sleep(2)

        # Close any drawer/modal that opened
        close_btn = await page.query_selector(
            'button[aria-label="Close"], [data-testid="close-button"]'
        )
        if close_btn:
            await close_btn.click()

        print(f"  Added to cart: {item['name']}")
        return True
    except Exception as e:
        print(f"  Could not add {item['name']} to cart: {e}")
        return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def run():
    if not ANTH_EMAIL or not ANTH_PASSWORD:
        print("Error: ANTH_EMAIL and ANTH_PASSWORD must be set in your .env file.")
        sys.exit(1)

    prices = load_prices()
    drops_added = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=HEADLESS)

        # Reuse saved session to avoid logging in every single run
        ctx_kwargs = {}
        if SESSION_FILE.exists():
            ctx_kwargs["storage_state"] = str(SESSION_FILE)

        context = await browser.new_context(
            **ctx_kwargs,
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        page = await context.new_page()

        await ensure_logged_in(page)

        # Persist the (possibly refreshed) session for next run
        await context.storage_state(path=str(SESSION_FILE))

        favorites = await scrape_favorites(page)

        for item in favorites:
            key = item["url"]
            stored = prices.get(key, {})
            prev_price = stored.get("price")
            curr_price = item["price"]

            if prev_price is None:
                print(f"  Tracking new item: {item['name']} @ ${curr_price:.2f}")
            elif curr_price < prev_price - PRICE_DROP_THRESHOLD:
                print(
                    f"  Price drop: {item['name']}  "
                    f"${prev_price:.2f} → ${curr_price:.2f}"
                )
                added = await add_to_cart(page, item)
                if added:
                    drops_added.append(
                        {
                            "name": item["name"],
                            "url": item["url"],
                            "old_price": prev_price,
                            "new_price": curr_price,
                        }
                    )
            else:
                print(f"  No change: {item['name']} @ ${curr_price:.2f}")

            prices[key] = {
                "name": item["name"],
                "price": curr_price,
                "last_checked": datetime.now().isoformat(),
            }

        await browser.close()

    save_prices(prices)

    if drops_added:
        print(f"\n{len(drops_added)} price drop(s) detected — sending notification...")
        send_notification(drops_added)
    else:
        print("\nNo price drops detected this run.")


if __name__ == "__main__":
    asyncio.run(run())
