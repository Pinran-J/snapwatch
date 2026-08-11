#!/usr/bin/env python3
"""
snap_watch.py — watch Eurostar Snap for London -> Paris fares and ping you when
the cheapest price changes (or drops below a threshold you set).

HOW IT WORKS
------------
Eurostar Snap (https://snap.eurostar.com) is a JavaScript app: the fares only
appear after you run a search, and they arrive over the network as JSON. So this
script drives a real (headless) browser with Playwright, submits the search for
each date you care about, and reads the prices straight from the network
responses (with a visible-text fallback). It remembers the last cheapest price
per date on disk and pings you only when something changes.

QUICK START
-----------
1. Install Python 3.10+ (required by current Playwright) then:
       pip install playwright requests
       playwright install chromium
2. Edit the CONFIG block below (dates, how you want to be pinged).
3. Run it:
       python snap_watch.py            # loops forever, checking on a schedule
       python snap_watch.py --once     # single check, then exit (good for cron)
       python snap_watch.py --once --show   # watch it work in a real window

FIRST RUN TIP
-------------
Set HEADLESS = False the first time so you can watch the browser. If the search
form doesn't fill correctly (Eurostar can change their markup), see
"IF THE FORM STOPS WORKING" near fill_search_form() — it's a 2-minute fix using
Playwright's codegen recorder.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import subprocess
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

# ---------------------------------------------------------------------------
# CONFIG — edit this block
# ---------------------------------------------------------------------------

ORIGIN = "London"
DESTINATION = "Paris"

# Which departure dates to watch. Snap sells up to ~10 days ahead, so watching
# far-future dates is pointless. Either list explicit dates:
#   DATES = ["2026-08-20", "2026-08-21"]
# ...or leave it as None to auto-watch the next N days (set DAYS_AHEAD below).
DATES = ["2026-08-14", "2026-08-15", "2026-08-16", "2026-08-17"]
DAYS_AHEAD = 7  # only used when DATES is None

PASSENGERS = 1

# Ping only when the cheapest fare is at or below this (in GBP). Set to None to
# ping on ANY change to the cheapest price.
PRICE_THRESHOLD: float | None = None

# Snap only sells a subset of dates, and blocked (sold-out / not-on-sale) dates
# show up greyed-out in the calendar. The script skips those cleanly and pings
# you when a previously-blocked date OPENS UP (that's the useful signal). Set
# this True if you also want a ping when an open date goes back to sold-out.
PING_ON_SOLD_OUT = False

# How often to check, in minutes. Please be considerate — Snap inventory doesn't
# move second-by-second, and hammering the site may get your IP rate-limited.
# 20–30 minutes is plenty. A little randomness is added on top.
CHECK_EVERY_MINUTES = 25

# How you want to be pinged: "discord", "telegram", "desktop", or "console".
#   discord  — easiest phone-capable ping. Make a Discord server, then
#              Channel > Edit > Integrations > Webhooks > New Webhook > Copy URL.
#   telegram — message BotFather to make a bot, then fill token + chat id.
#   desktop  — native notification on the machine running the script (mac/Linux
#              solid; Windows falls back to a console bell).
#   console  — just prints loudly. Good for testing.
NOTIFIER = "telegram"

DISCORD_WEBHOOK_URL = ""  # for NOTIFIER = "discord"
TELEGRAM_BOT_TOKEN = ""   # for NOTIFIER = "telegram" — set via TELEGRAM_BOT_TOKEN env/secret
TELEGRAM_CHAT_ID = ""     # for NOTIFIER = "telegram" — set via TELEGRAM_CHAT_ID env/secret

HEADLESS = True           # set False the first time to watch the browser
STATE_FILE = Path("snap_state.json")
DEBUG_DIR = Path("snap_debug")  # screenshots + captured API bodies land here

SNAP_URL = "https://snap.eurostar.com/uk-en"

# Environment variables override the settings above when present. This lets you
# keep secrets (token, chat id, webhook) OUT of the file when running in the
# cloud — you set them as encrypted secrets/env vars instead. Locally, if you
# don't set these, the values above are used.
NOTIFIER = os.environ.get("SNAP_NOTIFIER", NOTIFIER)
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", DISCORD_WEBHOOK_URL)
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", TELEGRAM_BOT_TOKEN)
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", TELEGRAM_CHAT_ID)

# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------

def get_telegram_chat_id() -> None:
    """
    Print your Telegram chat id. Run this AFTER sending your bot any message:
        1. Open Telegram, find your bot, send it "hi" (or press Start).
        2. Run:  python snap_watch.py --telegram-id
        3. Copy the number it prints into TELEGRAM_CHAT_ID (or your secret).
    """
    import requests
    if not TELEGRAM_BOT_TOKEN:
        print("Set TELEGRAM_BOT_TOKEN in the config (or env) first.")
        return
    try:
        data = requests.get(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates", timeout=20
        ).json()
    except Exception as exc:
        print(f"Couldn't reach Telegram: {exc}")
        return
    if not data.get("ok"):
        print("Telegram said:", data)
        return
    seen = {}
    for upd in data.get("result", []):
        msg = (upd.get("message") or upd.get("edited_message")
               or upd.get("channel_post") or {})
        chat = msg.get("chat") or {}
        if "id" in chat:
            seen[chat["id"]] = (chat.get("title") or chat.get("username")
                                or chat.get("first_name") or chat.get("type"))
    if not seen:
        print("No chat found yet. Send your bot a direct message first "
              "(open it in Telegram, type 'hi'), then run this again.")
        return
    print("Found chat id(s):")
    for cid, who in seen.items():
        print(f"    {cid}    ({who})")
    print("\nCopy the number into TELEGRAM_CHAT_ID (or your TELEGRAM_CHAT_ID secret).")


def notify(title: str, message: str) -> None:
    print(f"\n🔔 {title}\n   {message}\n")
    try:
        if NOTIFIER == "discord":
            _notify_discord(title, message)
        elif NOTIFIER == "telegram":
            _notify_telegram(title, message)
        elif NOTIFIER == "desktop":
            _notify_desktop(title, message)
        # "console" is already handled by the print above
    except Exception as exc:  # never let a failed ping crash the watcher
        print(f"   (couldn't send {NOTIFIER} notification: {exc})")


def _notify_discord(title: str, message: str) -> None:
    import requests
    requests.post(DISCORD_WEBHOOK_URL, json={"content": f"**{title}**\n{message}"}, timeout=20)


def _notify_telegram(title: str, message: str) -> None:
    import requests
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": f"{title}\n{message}"}, timeout=20)


def _notify_desktop(title: str, message: str) -> None:
    if sys.platform == "darwin":
        safe = message.replace('"', "'")
        subprocess.run(
            ["osascript", "-e", f'display notification "{safe}" with title "{title}" sound name "Ping"'],
            check=False,
        )
    elif sys.platform.startswith("linux"):
        subprocess.run(["notify-send", title, message], check=False)
    else:  # Windows / other: console bell fallback (use discord/telegram for real pings)
        try:
            print("\a", end="", flush=True)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Price extraction from arbitrary JSON
# ---------------------------------------------------------------------------

PRICE_KEY_RE = re.compile(r"(price|amount|fare|total|cost)", re.IGNORECASE)


def _harvest_prices(obj) -> list[float]:
    """Walk any nested JSON and pull out plausible price numbers."""
    found: list[float] = []

    def walk(node):
        if isinstance(node, dict):
            for key, val in node.items():
                if isinstance(val, (int, float)) and PRICE_KEY_RE.search(str(key)):
                    num = float(val)
                    # Some APIs express money in minor units (pence). Normalise
                    # obvious cases: a "price" of 5100 almost certainly means £51.
                    if num > 1000:
                        num = num / 100.0
                    if 5 <= num <= 500:  # sane Snap fare range
                        found.append(round(num, 2))
                else:
                    walk(val)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(obj)
    return found


SOLD_OUT_TEXT_RE = re.compile(r"no snap tickets are available", re.IGNORECASE)


def _extract_page_data(page) -> dict | None:
    """
    Pull Snap's own embedded page data (Next.js's `__NEXT_DATA__` blob) straight
    out of the results page. This is what the page itself used to render, so
    it's tied to exactly the date that was searched — unlike sniffing network
    responses, which can also carry fares for *other* dates (e.g. the little
    date-strip carousel shown above the results).
    """
    try:
        raw = page.locator("script#__NEXT_DATA__").text_content(timeout=5000)
        return json.loads(raw).get("props", {}).get("pageProps")
    except Exception:
        return None


def _fares_for_date(page_props: dict, date_iso: str) -> list[float]:
    """
    Prices for slots that are actually bookable on `date_iso`. Snap only
    attaches a `fare` to a time slot when it's available — sold-out slots have
    `fare: null` — so slots without one are correctly excluded rather than
    counted as available.
    """
    prices: list[float] = []
    for slot in page_props.get("outboundTimeSlots") or []:
        if not str(slot.get("id", "")).startswith(date_iso):
            continue  # defensive: only count slots for the requested date
        fare = slot.get("fare")
        if not fare:
            continue
        total = (fare.get("prices") or {}).get("total")
        if total is None:
            total = (fare.get("prices") or {}).get("displayPrice")
        if total is not None:
            prices.append(float(total))
    return sorted(set(prices))


# ---------------------------------------------------------------------------
# Browser flow
# ---------------------------------------------------------------------------

def dismiss_cookie_banner(page) -> None:
    for sel in ("#didomi-notice-agree-button", "button:has-text('Agree')",
                "button:has-text('Accept')", "button:has-text('OK')"):
        try:
            page.click(sel, timeout=2500)
            return
        except Exception:
            continue


class DateUnavailable(Exception):
    """Raised when the target date is greyed-out in the calendar (no Snap seats)."""


def _day_no_zero(d: date) -> str:
    return d.strftime("%#d") if sys.platform.startswith("win") else d.strftime("%-d")


def _date_label_variants(d: date) -> list[str]:
    """Accessible-name / aria-label forms Snap's calendar might use for a day."""
    day = _day_no_zero(d)
    return [
        f"{day} {d.strftime('%B %Y')}",                       # 17 August 2026
        f"{d.strftime('%B')} {day}, {d.strftime('%Y')}",      # August 17, 2026
        f"{d.strftime('%A')} {day} {d.strftime('%B %Y')}",    # Saturday 17 August 2026
        f"{d.strftime('%A, %B')} {day}, {d.strftime('%Y')}",  # Saturday, August 17, 2026
        f"{day} {d.strftime('%B')}",                          # 17 August
    ]


def _is_disabled(loc) -> bool:
    """True if a calendar cell is greyed-out / not selectable."""
    try:
        if not loc.is_visible():
            return True
        aria = (loc.get_attribute("aria-disabled") or "").lower()
        if aria == "true":
            return True
        if loc.get_attribute("disabled") is not None:
            return True
        cls = (loc.get_attribute("class") or "").lower()
        if any(w in cls for w in ("disabled", "unavailable", "blocked", "inactive")):
            return True
        return not loc.is_enabled()
    except Exception:
        return True


def select_date(page, d: date) -> None:
    """
    Open the calendar and pick date `d`. If that day is greyed-out (no Snap
    availability), raise DateUnavailable immediately instead of hanging.

    This is the part most likely to need adjusting if Eurostar changes their
    calendar markup — see the codegen note in fill_search_form's docstring.
    """
    # Open the picker. The field shows the current date (e.g. "Wed 12 Aug"),
    # so try a few likely triggers.
    for sel in ('button[aria-label*="date" i]', 'button[aria-label*="calendar" i]',
                'button[aria-label*="depart" i]', 'input[readonly]'):
        try:
            page.locator(sel).first.click(timeout=2000)
            break
        except Exception:
            continue

    # Advance months if the target month/year header isn't shown yet (near-term
    # dates are usually already visible, so this rarely fires).
    header = f"{d.strftime('%B')} {d.strftime('%Y')}"
    for _ in range(4):
        try:
            if page.get_by_text(header, exact=False).count():
                break
            page.locator(
                'button[aria-label*="next" i], [aria-label*="next month" i]'
            ).first.click(timeout=1500)
        except Exception:
            break

    # Find the day cell by its accessible name (most reliable), preferring a
    # button, then a gridcell.
    cell = None
    for label in _date_label_variants(d):
        pat = re.compile(re.escape(label), re.IGNORECASE)
        for role in ("button", "gridcell", "option"):
            cand = page.get_by_role(role, name=pat)
            if cand.count():
                cell = cand.first
                break
        if cell:
            break

    # Fallback: the bare day number, then climb to its clickable ancestor.
    if cell is None:
        num = page.get_by_text(re.compile(rf"^{d.day}$")).first
        if num.count() == 0:
            raise RuntimeError("couldn't locate the date cell in the calendar")
        cell = num.locator(
            "xpath=ancestor-or-self::*[self::button or @role='button' or @role='gridcell'][1]"
        )
        if cell.count() == 0:
            cell = num  # last resort: the number element itself

    if _is_disabled(cell):
        raise DateUnavailable(d.isoformat())

    cell.click(timeout=4000)


def fill_search_form(page, origin: str, destination: str, date_iso: str, passengers: int) -> None:
    """
    Enter the search on the Snap landing page.

    IF THE FORM STOPS WORKING
    -------------------------
    Eurostar occasionally changes their markup and one of these steps may fail.
    To get the exact up-to-date clicks, run:
        playwright codegen https://snap.eurostar.com/uk-en
    Do the search by hand in the window that opens; Playwright prints the exact
    lines of code as you click. Paste those in to replace the body of this
    function. Everything else in this script keeps working unchanged.
    """
    d = datetime.strptime(date_iso, "%Y-%m-%d").date()

    # Origin — the "from" field. Snap defaults it to London St Pancras, so this
    # only needs to change it when watching a route that doesn't start there.
    page.get_by_role("combobox").first.click(timeout=8000)
    page.get_by_role("combobox").first.fill(origin)
    try:
        page.get_by_role("option", name=re.compile(origin, re.IGNORECASE)).first.click(timeout=5000)
    except Exception:
        page.keyboard.press("Enter")

    # Destination — the "to" field is usually a combobox with an autocomplete list.
    page.get_by_role("combobox").last.click(timeout=8000)
    page.get_by_role("combobox").last.fill(destination)
    try:
        page.get_by_role("option", name=re.compile(destination, re.IGNORECASE)).first.click(timeout=5000)
    except Exception:
        page.keyboard.press("Enter")

    # Date — pick the day, or bail out fast if it's greyed-out (no Snap seats).
    select_date(page, d)

    # Passengers — only touch it if not the default of 1.
    if passengers and passengers != 1:
        try:
            page.get_by_role("button", name=re.compile("add|plus|passenger|\\+", re.IGNORECASE)).first.click(
                click_count=passengers - 1, timeout=4000)
        except Exception:
            pass

    # Submit
    page.get_by_role("button", name=re.compile("search", re.IGNORECASE)).first.click(timeout=8000)


def check_date(playwright, date_iso: str) -> dict:
    """Run one search and return {'date', 'cheapest', 'prices'}."""
    captured: list[float] = []
    api_hits: list[str] = []

    browser = playwright.chromium.launch(headless=HEADLESS)
    context = browser.new_context(locale="en-GB")
    page = context.new_page()

    def on_response(resp):
        ctype = resp.headers.get("content-type", "")
        if "application/json" not in ctype:
            return
        url = resp.url
        if not re.search(r"(api|snap|search|avail|fare|price|graphql)", url, re.IGNORECASE):
            return
        try:
            body = resp.json()
        except Exception:
            return
        prices = _harvest_prices(body)
        if prices:
            captured.extend(prices)
            api_hits.append(url)
            DEBUG_DIR.mkdir(exist_ok=True)
            stamp = re.sub(r"\W+", "_", url)[-60:]
            (DEBUG_DIR / f"api_{date_iso}_{stamp}.json").write_text(
                json.dumps(body, indent=2)[:200000], encoding="utf-8")

    page.on("response", on_response)

    # available: True = at least one bookable slot, False = date blocked/sold
    # out (either greyed-out before search, or "no tickets" after search),
    # None = errored.
    available = True
    prices: list[float] = []
    sold_out_after_search = False
    try:
        page.goto(SNAP_URL, wait_until="domcontentloaded", timeout=45000)
        dismiss_cookie_banner(page)
        fill_search_form(page, ORIGIN, DESTINATION, date_iso, PASSENGERS)
        # Give the fare responses time to arrive.
        page.wait_for_timeout(6000)
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass

        # Primary source: Snap's own embedded page data for this exact date —
        # correctly excludes fares for neighbouring dates and sold-out slots.
        page_props = _extract_page_data(page)
        if page_props is not None:
            prices = _fares_for_date(page_props, date_iso)

        sold_out_after_search = bool(page.get_by_text(SOLD_OUT_TEXT_RE).count())

        # Fallbacks (only used if the page data couldn't be read at all): the
        # sniffed network responses, then a DOM scrape for visible "£xx" text.
        # Skipped once we know the date is sold out, since both can pick up
        # prices for other dates shown elsewhere on the page (e.g. the date
        # strip), which would misreport a sold-out date as available.
        if page_props is None and not sold_out_after_search:
            if captured:
                prices = sorted(set(captured))
            else:
                text = page.content()
                for m in re.findall(r"£\s?(\d{1,3}(?:\.\d{2})?)", text):
                    val = float(m)
                    if 5 <= val <= 500:
                        prices.append(val)
                prices = sorted(set(prices))

        DEBUG_DIR.mkdir(exist_ok=True)
        page.screenshot(path=str(DEBUG_DIR / f"last_run_{date_iso}.png"), full_page=True)
    except DateUnavailable:
        available = False  # greyed-out in the calendar — expected, not an error
    except Exception as exc:
        available = None
        print(f"   [{date_iso}] search failed: {exc}")
        DEBUG_DIR.mkdir(exist_ok=True)
        try:
            page.screenshot(path=str(DEBUG_DIR / f"error_{date_iso}.png"), full_page=True)
        except Exception:
            pass
    finally:
        context.close()
        browser.close()

    if available is not False and sold_out_after_search:
        available = False
        prices = []

    cheapest = prices[0] if prices else None
    if api_hits:
        print(f"   [{date_iso}] read fares from: {api_hits[0]}")
    return {"date": date_iso, "cheapest": cheapest, "prices": prices, "available": available}


# ---------------------------------------------------------------------------
# State + orchestration
# ---------------------------------------------------------------------------

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            return {}
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


def target_dates() -> list[str]:
    if DATES:
        return DATES
    today = date.today()
    return [(today + timedelta(days=i)).isoformat() for i in range(1, DAYS_AHEAD + 1)]


def run_once() -> None:
    from playwright.sync_api import sync_playwright

    state = load_state()
    dates = target_dates()
    print(f"Checking {ORIGIN} -> {DESTINATION} for {len(dates)} date(s) "
          f"at {datetime.now():%H:%M:%S}")

    with sync_playwright() as pw:
        for i, d in enumerate(dates):
            result = check_date(pw, d)
            avail = result["available"]
            cheapest = result["cheapest"]

            prev = state.get(d, {})
            first_sight = not prev
            prev_avail = prev.get("available")
            prev_cheapest = prev.get("cheapest")

            if avail is None:
                print(f"   [{d}] check errored — see snap_debug/error_{d}.png")
            elif avail is False:
                print(f"   [{d}] blocked / no Snap seats on this date")
                if PING_ON_SOLD_OUT and prev_avail is True:
                    notify(f"Snap {ORIGIN}→{DESTINATION} {d}: sold out",
                           "This date is no longer bookable on Snap.")
            elif cheapest is None:
                print(f"   [{d}] available, but couldn't read a price "
                      f"(see snap_debug/last_run_{d}.png)")
            else:
                was_open = prev_avail is True and prev_cheapest is not None
                note = (f" (was £{prev_cheapest:.2f})" if was_open
                        else " (just opened up)" if not first_sight else "")
                print(f"   [{d}] cheapest £{cheapest:.2f}{note}")

                meets = PRICE_THRESHOLD is None or cheapest <= PRICE_THRESHOLD
                title = f"Snap {ORIGIN}→{DESTINATION} {d}: £{cheapest:.2f}"
                msg = None

                if not was_open:
                    # Date just became bookable (the signal you care about).
                    if meets and not first_sight:
                        msg = f"Tickets just opened for this date! Book: {SNAP_URL}"
                    elif meets and first_sight and PRICE_THRESHOLD is not None:
                        msg = f"At/below your £{PRICE_THRESHOLD:.0f} target. Book: {SNAP_URL}"
                    # First-ever sight with no threshold: stay quiet, set a baseline.
                else:
                    if PRICE_THRESHOLD is not None:
                        if meets and cheapest < prev_cheapest:
                            msg = (f"Dropped £{prev_cheapest:.2f} → £{cheapest:.2f}, "
                                   f"at/below your £{PRICE_THRESHOLD:.0f} target. Book: {SNAP_URL}")
                    elif cheapest != prev_cheapest:
                        arrow = "dropped" if cheapest < prev_cheapest else "changed"
                        msg = f"Price {arrow} £{prev_cheapest:.2f} → £{cheapest:.2f}. Book: {SNAP_URL}"

                if msg:
                    notify(title, msg)

            state[d] = {"available": avail, "cheapest": cheapest,
                        "prices": result["prices"],
                        "checked": datetime.now().isoformat(timespec="seconds")}
            save_state(state)

            # be polite between dates
            if i < len(dates) - 1:
                time.sleep(random.uniform(3, 7))

    # forget dates that are now in the past
    for d in list(state.keys()):
        try:
            if datetime.strptime(d, "%Y-%m-%d").date() < date.today():
                del state[d]
        except ValueError:
            pass
    save_state(state)


def run_forever() -> None:
    print(f"Watcher started. Checking every ~{CHECK_EVERY_MINUTES} min. Ctrl+C to stop.")
    while True:
        try:
            run_once()
        except Exception as exc:
            print(f"Check failed, will retry next cycle: {exc}")
        wait = CHECK_EVERY_MINUTES * 60 + random.uniform(0, 180)
        print(f"Next check at {datetime.now() + timedelta(seconds=wait):%H:%M:%S}\n")
        time.sleep(wait)


def main() -> None:
    parser = argparse.ArgumentParser(description="Watch Eurostar Snap fares.")
    parser.add_argument("--once", action="store_true", help="check once and exit")
    parser.add_argument("--show", action="store_true", help="show the browser window")
    parser.add_argument("--telegram-id", action="store_true",
                        help="print your Telegram chat id and exit")
    args = parser.parse_args()

    if args.telegram_id:
        get_telegram_chat_id()
        return

    if args.show:
        global HEADLESS
        HEADLESS = False

    if args.once:
        run_once()
    else:
        run_forever()


if __name__ == "__main__":
    main()
