"""
Polls a Google Sheet of USERNAME/PASSWORD/NEW PASSWORD rows and changes each
account's password via main.run_change_account_password() (same mechanism
the Telegram bot's /cp command uses -- see CLAUDE.md's "/cp: changing the
password on an EXISTING account" section).

Sheet layout (row 1 = header):

    A: USERNAME   B: PASSWORD   C: NEW PASSWORD   D: STATUS

Same queue semantics as balance_checker.py/sheet_watcher.py: a row with A+B
filled and an EMPTY STATUS is picked up, processed once, and STATUS is then
set to a result -- it won't be picked up again on the next poll. Clear a
row's STATUS cell by hand to re-run it (e.g. after fixing a typo'd password).
C (NEW PASSWORD) is optional -- if left blank, a random policy-compliant
password is generated (main.gen_password(), same as /cp) and WRITTEN BACK
into column C before the change is attempted, so the sheet itself ends up
holding the actual new password either way.

Setup (one-time -- reuse the same service_account.json already made for
sheet_watcher.py/balance_checker.py if you have one; the steps are
identical): see balance_checker.py's own docstring.

Run:
    PASSWORD_SHEET_SPREADSHEET_ID=<your sheet id> \\
        .venv/bin/python password_changer.py --env .env.password

Reuses --env's BOT_SITE_URL and SETTINGS_FILE (so it automatically picks up
whatever proxy /setproxy currently has set on that bot instance), same
convention as sheet_watcher.py/balance_checker.py.

Each row goes through main.run_change_account_password() -- a real
Playwright login (there is no HTTP-fast path for this endpoint, unlike
balance_checker.py's http_check_account_balance() shortcut; it hasn't been
investigated whether change_account_password()'s request works from a bare
requests.Session without the cookies a real browser session accumulates,
the same open question free_phone_number()'s HTTP path has). MAX_CONCURRENT
defaults to 1 and _wait_for_turn() paces every change (default
CHECK_SPACING_SECONDS=30s) for the same reason balance_checker.py added
both: cricmatch247's edge-level rate block reacts to LOGIN volume/
concurrency from one IP, and this script logs in just as much as that one
does per row.
"""
import json
import os
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor

# --env <path> selects which env file to load, same convention as
# telegram_bot.py/sheet_watcher.py/balance_checker.py -- lets this run
# against a specific bot instance's site/proxy settings without duplicating
# them into a new file.
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
from sites import profile_for
load_dotenv(_env_file, override=True)

SPREADSHEET_ID = os.environ.get("PASSWORD_SHEET_SPREADSHEET_ID", "")
WORKSHEET_GID = os.environ.get("PASSWORD_SHEET_WORKSHEET_GID", "0")
CREDENTIALS_FILE = os.environ.get(
    "PASSWORD_SHEET_CREDENTIALS_FILE", os.environ.get("SHEET_CREDENTIALS_FILE", "service_account.json"))
POLL_SECONDS = int(os.environ.get("PASSWORD_SHEET_POLL_SECONDS", "20"))
# See module docstring -- kept conservative like balance_checker.py's
# MAX_CONCURRENT/CHECK_SPACING_SECONDS, not yet confirmed via a controlled
# test against this specific endpoint.
MAX_CONCURRENT = int(os.environ.get("PASSWORD_SHEET_MAX_CONCURRENT", "1"))
CHECK_SPACING_SECONDS = int(os.environ.get("PASSWORD_SHEET_CHECK_SPACING_SECONDS", "30"))
SITE_URL = os.environ.get("BOT_SITE_URL") or engine.SITE_URL
SETTINGS_FILE = os.environ.get("SETTINGS_FILE", "bot_settings.json")

COL_USERNAME, COL_PASSWORD, COL_NEW_PASSWORD, COL_STATUS = range(1, 5)

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
    """Mirrors balance_checker.py's current_proxy() -- reads the bot's own
    SETTINGS_FILE live on each change, so /setproxy on that Telegram bot
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


def process_row(ws, row_idx, username, current_password, new_password):
    _wait_for_turn()
    print(f"[row {row_idx}] changing password for {username}...")

    if not new_password:
        new_password = engine.gen_password()
        try:
            ws.update_cell(row_idx, COL_NEW_PASSWORD, new_password)
        except Exception:
            traceback.print_exc()

    result = {"ok": False, "messages": [], "shot": None, "new_password": None}
    try:
        # Same shape as balance_checker.process_row(): take the ~2s HTTP route
        # where the site is confirmed to accept an out-of-band change-password
        # POST, otherwise fall back to the ~30s Playwright login. NOT keyed on
        # supports_change_password -- cricmatch supports the endpoint but only
        # from an in-page fetch(), which is why the flag is separate.
        prof = profile_for(SITE_URL)
        if prof.supports_http_change_password:
            result = engine.http_change_account_password(
                username, current_password, new_password,
                site_url=SITE_URL, proxy=current_proxy())
        else:
            result = engine.run_change_account_password(
                username, current_password, new_password, site_url=SITE_URL, proxy=current_proxy())
    except Exception as e:
        traceback.print_exc()
        result["messages"] = [f"Unhandled error: {e}"]

    ts = time.strftime("%Y-%m-%d %H:%M")
    try:
        if result["ok"]:
            ws.update_cell(row_idx, COL_STATUS, f"✅ changed {ts}")
        elif result.get("infra_block"):
            # The edge answered, not the app -- this account was never actually
            # checked, so record it as retryable (⏳) rather than burning the
            # row on a failure that never happened. poll_once() picks ⏳ rows
            # back up; ✅/❌ are terminal.
            msg = "; ".join(result.get("messages") or ["blocked"])[:150]
            ws.update_cell(row_idx, COL_STATUS, f"⏳ blocked {ts} — {msg}")
        else:
            msg = "; ".join(result.get("messages") or ["unknown error"])[:200]
            ws.update_cell(row_idx, COL_STATUS, f"❌ {ts} — {msg}")
    except Exception:
        traceback.print_exc()

    with _lock:
        _in_flight_rows.discard(row_idx)
    print(f"[row {row_idx}] done: ok={result['ok']}")


def poll_once(ws, executor):
    rows = ws.get_all_values()
    for i, row in enumerate(rows[1:], start=2):  # row 1 is the header
        row = row + [""] * (4 - len(row))
        username = row[0].strip()
        current_password = row[1]
        new_password = row[2].strip()
        status = row[3].strip()
        if not (username and current_password):
            continue
        # Empty STATUS (never attempted) or a "⏳" marker (the edge blocked an
        # earlier attempt, so nothing was actually changed) are both eligible.
        # Any other non-empty STATUS is terminal -- clear it by hand to retry.
        if status and not status.startswith("⏳"):
            continue
        with _lock:
            if i in _in_flight_rows:
                continue  # already picked up this cycle, still running
            _in_flight_rows.add(i)
        executor.submit(process_row, ws, i, username, current_password, new_password)


def main():
    if not SPREADSHEET_ID:
        print("PASSWORD_SHEET_SPREADSHEET_ID is not set -- nothing to poll. "
              "Set it to the sheet's id (from its URL) and try again.")
        sys.exit(1)
    print(f"password_changer: spreadsheet={SPREADSHEET_ID} gid={WORKSHEET_GID} "
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
