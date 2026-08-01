"""
Polls a Google Sheet of USERNAME/PASSWORD rows and frees each account's
phone number via main.run_free_account_number() (same mechanism the
Telegram bot's /freenum command uses -- see CLAUDE.md's "/freenum: freeing
the phone number on an EXISTING account, on demand" section). Logs into
each account, swaps its mobile number to a random new one, and writes that
new number back into the sheet -- e.g. to free up a batch of real numbers
for reuse in future signups, without running /freenum one at a time in chat.

Sheet layout (row 1 = header):

    A: USERNAME   B: PASSWORD   C: NEW PHONE   D: STATUS

Same queue semantics as balance_checker.py/password_changer.py: a row with
A+B filled and an EMPTY STATUS is picked up, processed once, and STATUS is
then set to a result -- it won't be picked up again on the next poll. Clear
a row's STATUS cell by hand to re-run it. C (NEW PHONE) is output-only
(there's no "requested" phone number here, unlike password_changer.py's
optional NEW PASSWORD column) -- it's written back only on success, with
whatever random number free_phone_number() generated and confirmed.

Setup (one-time -- reuse the same service_account.json already made for
sheet_watcher.py/balance_checker.py/password_changer.py if you have one;
the steps are identical): see balance_checker.py's own docstring.

Run:
    FREE_NUMBER_SHEET_SPREADSHEET_ID=<your sheet id> \\
        .venv/bin/python phone_freer.py --env .env.khelofun

Reuses --env's BOT_SITE_URL and SETTINGS_FILE (so it automatically picks up
whatever proxy /setproxy currently has set on that bot instance), same
convention as sheet_watcher.py/balance_checker.py/password_changer.py.

Each row goes through main.run_free_account_number() -- a real Playwright
login (same as password_changer.py; there is no HTTP-fast path for the
free-number endpoint used here). MAX_CONCURRENT defaults to 1 and
_wait_for_turn() paces every change (default CHECK_SPACING_SECONDS=30s) for
the same reason balance_checker.py/password_changer.py added both:
cricmatch247's (and, by inference, khelofun's -- same platform) edge-level
rate block reacts to LOGIN volume/concurrency from one IP, and this script
logs in just as much per row as those do. free_phone_number() itself
already retries internally up to FREE_NUMBER_MAX_ATTEMPTS (15, ~45s apart --
see main.py) for the free-number call specifically; this script's own
pacing/backoff is a separate, outer layer covering the LOGIN step before
that retry loop even starts.
"""
import json
import os
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor

# --env <path> selects which env file to load, same convention as
# telegram_bot.py/sheet_watcher.py/balance_checker.py/password_changer.py --
# lets this run against a specific bot instance's site/proxy settings
# without duplicating them into a new file.
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

SPREADSHEET_ID = os.environ.get("FREE_NUMBER_SHEET_SPREADSHEET_ID", "")
WORKSHEET_GID = os.environ.get("FREE_NUMBER_SHEET_WORKSHEET_GID", "0")
CREDENTIALS_FILE = os.environ.get(
    "FREE_NUMBER_SHEET_CREDENTIALS_FILE", os.environ.get("SHEET_CREDENTIALS_FILE", "service_account.json"))
POLL_SECONDS = int(os.environ.get("FREE_NUMBER_SHEET_POLL_SECONDS", "20"))
# See module docstring -- kept conservative like balance_checker.py's /
# password_changer.py's MAX_CONCURRENT/CHECK_SPACING_SECONDS, not yet
# confirmed via a controlled test against this specific endpoint+site.
MAX_CONCURRENT = int(os.environ.get("FREE_NUMBER_SHEET_MAX_CONCURRENT", "1"))
CHECK_SPACING_SECONDS = int(os.environ.get("FREE_NUMBER_SHEET_CHECK_SPACING_SECONDS", "30"))
SITE_URL = os.environ.get("BOT_SITE_URL") or engine.SITE_URL
SETTINGS_FILE = os.environ.get("SETTINGS_FILE", "bot_settings.json")

COL_USERNAME, COL_PASSWORD, COL_NEW_PHONE, COL_STATUS = range(1, 5)

_lock = threading.Lock()
_in_flight_rows = set()
_spacing_lock = threading.Lock()
_last_check_started = 0.0


def _wait_for_turn():
    """Blocks until at least CHECK_SPACING_SECONDS has passed since the last
    change STARTED (not finished) -- called from the worker thread(s)
    actually running process_row, so it paces logins without blocking
    poll_once() itself from noticing new rows."""
    global _last_check_started
    with _spacing_lock:
        wait = CHECK_SPACING_SECONDS - (time.time() - _last_check_started)
        if wait > 0:
            time.sleep(wait)
        _last_check_started = time.time()


def current_proxy():
    """Mirrors balance_checker.py's/password_changer.py's current_proxy() --
    reads the bot's own SETTINGS_FILE live on each change, so /setproxy on
    that Telegram bot applies here with no separate config."""
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
    _wait_for_turn()
    print(f"[row {row_idx}] freeing number for {username}...")

    result = {"ok": False, "messages": [], "shot": None, "freed_phone": None}
    try:
        result = engine.run_free_account_number(username, password, site_url=SITE_URL, proxy=current_proxy())
    except Exception as e:
        traceback.print_exc()
        result["messages"] = [f"Unhandled error: {e}"]

    ts = time.strftime("%Y-%m-%d %H:%M")
    try:
        if result["ok"]:
            ws.update_cell(row_idx, COL_NEW_PHONE, result["freed_phone"])
            ws.update_cell(row_idx, COL_STATUS, f"✅ freed {ts}")
        else:
            msg = "; ".join(result.get("messages") or ["unknown error"])[:200]
            ws.update_cell(row_idx, COL_STATUS, f"❌ {ts} — {msg}")
    except Exception:
        traceback.print_exc()

    with _lock:
        _in_flight_rows.discard(row_idx)
    print(f"[row {row_idx}] done: ok={result['ok']} freed_phone={result.get('freed_phone')}")


def poll_once(ws, executor):
    rows = ws.get_all_values()
    for i, row in enumerate(rows[1:], start=2):  # row 1 is the header
        row = row + [""] * (4 - len(row))
        username = row[0].strip()
        password = row[1]
        status = row[3].strip()
        if not (username and password):
            continue
        if status:
            continue  # already processed -- clear STATUS by hand to retry
        with _lock:
            if i in _in_flight_rows:
                continue  # already picked up this cycle, still running
            _in_flight_rows.add(i)
        executor.submit(process_row, ws, i, username, password)


def main():
    if not SPREADSHEET_ID:
        print("FREE_NUMBER_SHEET_SPREADSHEET_ID is not set -- nothing to poll. "
              "Set it to the sheet's id (from its URL) and try again.")
        sys.exit(1)
    print(f"phone_freer: spreadsheet={SPREADSHEET_ID} gid={WORKSHEET_GID} "
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
        print("\nStopping (waiting for in-flight changes to finish)...")
    finally:
        executor.shutdown(wait=True)


if __name__ == "__main__":
    main()
