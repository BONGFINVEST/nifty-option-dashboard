"""
Keep-alive visitor for the NIFTY OI Scanner Streamlit app.

Why this exists (not just a cron ping):
  A plain HTTP GET to a sleeping Streamlit Community Cloud app returns a
  200 with a static HTML shell -- the page's JavaScript never runs, so the
  WebSocket that actually starts your Python script never opens, and the
  "Yes, get this app back up!" button never gets clicked. A real headless
  browser is required to genuinely wake the app and let one live poll
  (fetch -> analyze -> save to Google Sheets) actually execute.

This script:
  1. Confirms it's real NSE trading hours (IST, Mon-Fri, 09:15-15:30) before
     doing anything -- outside that window it exits immediately so the
     GitHub Actions cron (deliberately a bit wider, for schedule drift
     margin) doesn't waste minutes or hit Dhan's API when the market's shut.
  2. Opens the app in headless Chromium.
  3. If the "app is sleeping" screen appears, clicks the wake button.
  4. Waits long enough for one Streamlit script run (fetch + analysis +
     Google Sheet write) to complete before closing.
"""

import os
import sys
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

from playwright.sync_api import sync_playwright

IST = ZoneInfo("Asia/Kolkata")
NSE_OPEN = dtime(9, 15)
NSE_CLOSE = dtime(15, 40)

APP_URL = os.environ.get("STREAMLIT_APP_URL", "").strip()
WAIT_FOR_POLL_CYCLE_MS = 25_000  # give the app time to fetch+save before we disconnect


def within_market_hours() -> bool:
    now_ist = datetime.now(IST)
    if now_ist.weekday() >= 5:
        print(f"Weekend ({now_ist:%A}) — skipping.")
        return False
    if not (NSE_OPEN <= now_ist.time() <= NSE_CLOSE):
        print(f"Outside market hours (IST now: {now_ist:%H:%M:%S}) — skipping.")
        return False
    return True


def main():
    if not APP_URL:
        print("ERROR: STREAMLIT_APP_URL secret is not set.", file=sys.stderr)
        sys.exit(1)

    if not within_market_hours():
        sys.exit(0)  # not an error -- just nothing to do right now

    print(f"Market hours confirmed (IST {datetime.now(IST):%H:%M:%S}). Visiting {APP_URL} ...")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        try:
            page.goto(APP_URL, timeout=60_000, wait_until="domcontentloaded")

            # If the app is asleep, click the wake button.
            try:
                wake_button = page.get_by_text("Yes, get this app back up!", exact=False)
                wake_button.wait_for(timeout=8_000)
                print("App was asleep — clicking wake button.")
                wake_button.click()
            except Exception:
                print("No sleep screen detected — app was already awake.")

            # Give the script time to actually run: connect, fetch the option
            # chain, run the analysis, and (if due) write to Google Sheets.
            page.wait_for_timeout(WAIT_FOR_POLL_CYCLE_MS)
            print("Visit complete — one live poll cycle should have run.")

        except Exception as e:
            print(f"WARNING: visit did not complete cleanly: {e}", file=sys.stderr)
            # Don't hard-fail the whole workflow over a single flaky visit —
            # the next scheduled run 10 minutes later will just try again.
        finally:
            browser.close()


if __name__ == "__main__":
    main()
