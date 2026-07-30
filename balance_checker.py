"""
Polls a Google Sheet of account username/password pairs and keeps a BALANCE
column updated by logging into each account periodically. Read-only against
the accounts themselves -- it never places a bet, only reads the site's own
wallet balance (see read_wallet_balance()/check_account_balance() in
main.py).

Sheet layout (row 1 = header):

    A: USERNAME   B: PASSWORD   C: BALANCE   D: STATUS

Unlike sheet_watcher.py's hedge queue, there's no "already done, skip it"
state here -- every row with A+B filled gets re-checked on every poll cycle,
since a balance is only useful if it keeps refreshing. STATUS shows the
outcome of the most recent check ("checked <timestamp>" or an error);
BALANCE holds the last SUCCESSFULLY read number and is left alone on a
failed check, so a transient login hiccup or a WAF block doesn't blank out
the last known-good value.

Setup (one-time -- reuse the same service_account.json already made for
sheet_watcher.py if you have one; the steps are identical):
    .venv/bin/pip install gspread google-auth   # already in requirements.txt
    Create a Google Cloud project -> enable the Google Sheets API -> create a
    service account -> download its JSON key -> share the SHEET with the
    service account's email (Editor access, since BALANCE/STATUS get written
    back, not just read).

Run:
    BALANCE_SHEET_SPREADSHEET_ID=<your sheet id> \\
        .venv/bin/python balance_checker.py --env .env.cricmatch

Reuses --env's BOT_SITE_URL and SETTINGS_FILE (so it automatically picks up
whatever proxy /setproxy currently has set on that bot instance), same
convention as sheet_watcher.py.

Each row is checked via main.http_check_account_balance() -- two plain HTTP
POSTs (login, then the site's own getBalance() call) via `requests`, no
browser at all -- when the resolved site's profile sets
supports_http_login=True (cricmatch only, confirmed live 2026-07-30, see
CLAUDE.md's "Sheet-driven balance checking" section). This is ~10x faster
per account than the old Playwright-login path (run_balance_check(), still
used as the automatic fallback for any site that doesn't support it) -- but
MAX_CONCURRENT still defaults to 1, not higher, since every account here
typically shares one proxy IP and concurrent login POSTs from the same IP
are what trip the site's rate-based block (see MAX_CONCURRENT's own comment
below). The speed win shows up as a fast serialized sweep through the whole
sheet, not as parallelism.
"""
import json
import os
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor

# --env <path> selects which env file to load, same convention as
# telegram_bot.py/sheet_watcher.py -- lets this run against a specific bot
# instance's site/proxy settings without duplicating them into a new file.
_env_file = ".env"
if "--env" in sys.argv:
    _idx = sys.argv.index("--env")
    if _idx + 1 < len(sys.argv):
        _env_file = sys.argv[_idx + 1]
ONCE = "--once" in sys.argv

from dotenv import load_dotenv

import gspread
from google.oauth2.service_account import Credentials

# main.py runs its own bare load_dotenv() at import time (loads the repo's
# plain .env); override=True here so an explicit --env file wins for any key
# both files define, same gotcha documented in telegram_bot.py/sheet_watcher.py.
import main as engine
load_dotenv(_env_file, override=True)

SPREADSHEET_ID = os.environ.get("BALANCE_SHEET_SPREADSHEET_ID", "")
WORKSHEET_GID = os.environ.get("BALANCE_SHEET_WORKSHEET_GID", "0")
CREDENTIALS_FILE = os.environ.get(
    "BALANCE_SHEET_CREDENTIALS_FILE", os.environ.get("SHEET_CREDENTIALS_FILE", "service_account.json"))
# Logins are heavier than a queue poll (real page load + real login, easily
# 15-30s+ each) and this site rate-limits/WAF-blocks aggressive automated
# traffic (see CLAUDE.md) -- default to a much longer interval than
# sheet_watcher.py's 20s queue poll.
# Default 1, NOT higher, even though the HTTP-fast path is cheap enough to
# run many at once: every account here shares the SAME proxy IP (one
# BALANCE_SHEET, one /setproxy), so N "concurrent" checks means N login POSTs
# hitting the site from that one IP in the same instant. Confirmed live
# 2026-07-30 that this is exactly what trips cricmatch247's edge-level rate
# block (a bare 403 on /login, same category documented elsewhere in
# CLAUDE.md for /register and /send_otp_touser) -- a batch of 21 accounts at
# MAX_CONCURRENT=5 got every single row blocked, and repeating the poll made
# it worse, not better, since each retry re-triggered the same burst. A
# serialized run of the same 21 accounts (~3s each) still finishes in ~1
# minute, well inside the default 300s poll window, so there's no real
# throughput reason to raise this unless a future proxy setup gives each
# account its own IP.
POLL_SECONDS = int(os.environ.get("BALANCE_POLL_SECONDS", "300"))
MAX_CONCURRENT = int(os.environ.get("BALANCE_MAX_CONCURRENT", "1"))
SITE_URL = os.environ.get("BOT_SITE_URL") or engine.SITE_URL
SETTINGS_FILE = os.environ.get("SETTINGS_FILE", "bot_settings.json")

COL_USERNAME, COL_PASSWORD, COL_BALANCE, COL_STATUS = range(1, 5)

_lock = threading.Lock()
_in_flight_rows = set()


def current_proxy():
    """Mirrors sheet_watcher.py's current_proxy() -- reads the bot's own
    SETTINGS_FILE live on each check, so /setproxy on that Telegram bot
    applies here with no separate config."""
    try:
        with open(SETTINGS_FILE) as f:
            return json.load(f).get("proxy")
    except Exception:
        return None


def get_worksheet():
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=scopes)
    client = gspread.authorize(creds)
    sh = client.open_by_key(SPREADSHEET_ID)
    for ws in sh.worksheets():
        if str(ws.id) == str(WORKSHEET_GID):
            return ws
    return sh.sheet1


def process_row(ws, row_idx, username, password):
    print(f"[row {row_idx}] checking {username}...")
    result = {"ok": False, "balance": None, "messages": [], "shot": None}
    prof = engine.profile_for(SITE_URL)
    checker = engine.http_check_account_balance if prof.supports_http_login else engine.run_balance_check
    try:
        result = checker(username, password, site_url=SITE_URL, proxy=current_proxy())
    except Exception as e:
        traceback.print_exc()
        result["messages"] = [f"Unhandled error: {e}"]

    ts = time.strftime("%Y-%m-%d %H:%M")
    try:
        if result["ok"]:
            ws.update_cell(row_idx, COL_BALANCE, result["balance"])
            ws.update_cell(row_idx, COL_STATUS, f"✅ checked {ts}")
        else:
            # Leave the last-known BALANCE cell alone -- a login hiccup or a
            # WAF block shouldn't blank out the last good reading.
            msg = "; ".join(result.get("messages") or ["unknown error"])[:200]
            ws.update_cell(row_idx, COL_STATUS, f"❌ {ts} — {msg}")
    except Exception:
        traceback.print_exc()

    with _lock:
        _in_flight_rows.discard(row_idx)
    print(f"[row {row_idx}] done: ok={result['ok']} balance={result.get('balance')}")


def poll_once(ws, executor):
    rows = ws.get_all_values()
    for i, row in enumerate(rows[1:], start=2):  # row 1 is the header
        row = row + [""] * (4 - len(row))
        username, password = row[0].strip(), row[1]
        if not (username and password):
            continue
        with _lock:
            if i in _in_flight_rows:
                continue  # still being checked from a previous cycle
            _in_flight_rows.add(i)
        executor.submit(process_row, ws, i, username, password)


def main():
    if not SPREADSHEET_ID:
        print("BALANCE_SHEET_SPREADSHEET_ID is not set -- nothing to poll. "
              "Set it to the sheet's id (from its URL) and try again.")
        sys.exit(1)
    print(f"balance_checker: spreadsheet={SPREADSHEET_ID} gid={WORKSHEET_GID} "
          f"site={SITE_URL} poll={POLL_SECONDS}s max_concurrent={MAX_CONCURRENT}")
    ws = get_worksheet()
    executor = ThreadPoolExecutor(max_workers=MAX_CONCURRENT)
    try:
        while True:
            try:
                poll_once(ws, executor)
            except Exception:
                traceback.print_exc()
            if ONCE:
                break
            time.sleep(POLL_SECONDS)
    except KeyboardInterrupt:
        print("\nStopping (waiting for in-flight checks to finish)...")
    finally:
        executor.shutdown(wait=True)


if __name__ == "__main__":
    main()
