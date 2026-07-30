"""
Polls a Google Sheet of account username/password pairs and keeps a BALANCE
column updated by logging into each account periodically. Read-only against
the accounts themselves -- it never places a bet, only reads the site's own
wallet balance (see read_wallet_balance()/check_account_balance() in
main.py).

Sheet layout (row 1 = header):

    A: USERNAME   B: PASSWORD   C: BALANCE   D: STATUS

Same queue semantics as sheet_watcher.py's hedge sheet, NOT "re-check
everything forever" (an earlier version did that -- see git history --
and it turned out to be exactly what was tripping cricmatch247's login
rate-block, since a full sheet got hit with a fresh burst of logins on
every single poll cycle): a row with A+B filled and an EMPTY STATUS is
picked up, checked once, and STATUS is then set to a result -- which also
means it won't be picked up again on the next poll. Add a new row -> it
gets checked on the very next poll (POLL_SECONDS is short specifically so
this feels close to instant). To force a re-check of an existing row,
clear its STATUS cell by hand. BALANCE holds the last SUCCESSFULLY read
number and is left alone on a failed check, so a transient login hiccup or
a WAF block doesn't blank out the last known-good value -- but note STATUS
still gets written on failure, so a failed row does NOT get retried
automatically; clear STATUS to try again.

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

_wait_for_turn() additionally paces every check (default
CHECK_SPACING_SECONDS=30, see its comment) -- real 2026-07-30 sheet data
showed serialized-but-rapid checks (~9s apart, no deliberate delay) still
tripped the same rate block once a burst of ~20 new rows got processed in a
few minutes, so MAX_CONCURRENT=1 alone wasn't the whole fix.
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
# Now that a poll only ever fires a real login for rows with an EMPTY
# STATUS (see poll_once()) -- not the whole sheet every cycle -- most polls
# do nothing but a cheap get_all_values() read, so this can go back to a
# short interval like sheet_watcher.py's 20s without hammering the login
# endpoint at all. A brand-new row gets checked within one poll interval of
# being added, which is the "instant" behavior this was changed to get.
POLL_SECONDS = int(os.environ.get("BALANCE_POLL_SECONDS", "20"))
# Still defaults to 1, not higher: every account here typically shares one
# proxy IP, and several NEW rows added at once would otherwise fire that
# many concurrent login POSTs from the same IP in the same instant --
# confirmed live 2026-07-30 that concurrent logins from one IP (not just
# rapid sequential ones) trip cricmatch247's edge-level rate block (a bare
# 403 on /login, same category documented in CLAUDE.md for /register and
# /send_otp_touser). A serialized queue of new rows still clears in ~3s each,
# so there's no real throughput reason to raise this unless a future proxy
# setup gives each account its own IP.
MAX_CONCURRENT = int(os.environ.get("BALANCE_MAX_CONCURRENT", "1"))
# Real sheet data 2026-07-30: MAX_CONCURRENT=1 (already fully serialized)
# was NOT enough on its own -- a batch of ~20 new rows checked back-to-back
# (~9s apart, no deliberate pacing) all succeeded, but the very next few rows
# added right after immediately hit the same 403. That points to a VOLUME
# limit (something like "~20 logins in a few minutes from one IP"), not a
# concurrency one -- serialized-but-rapid is still rapid. This forces a
# minimum gap between the START of each login attempt, spreading a burst of
# many new rows out over minutes instead of firing them as fast as the
# executor can. Exact safe threshold is NOT confirmed (no controlled test
# run yet) -- 30s is a conservative starting guess, tuneable via env var.
CHECK_SPACING_SECONDS = int(os.environ.get("BALANCE_CHECK_SPACING_SECONDS", "30"))
SITE_URL = os.environ.get("BOT_SITE_URL") or engine.SITE_URL
SETTINGS_FILE = os.environ.get("SETTINGS_FILE", "bot_settings.json")

COL_USERNAME, COL_PASSWORD, COL_BALANCE, COL_STATUS = range(1, 5)

_lock = threading.Lock()
_in_flight_rows = set()
_spacing_lock = threading.Lock()
_last_check_started = 0.0


def _wait_for_turn():
    """Blocks until at least CHECK_SPACING_SECONDS has passed since the last
    check STARTED (not finished) -- called from the worker thread(s) actually
    running process_row, so it paces logins without blocking poll_once()
    itself from noticing new rows."""
    global _last_check_started
    with _spacing_lock:
        wait = CHECK_SPACING_SECONDS - (time.time() - _last_check_started)
        if wait > 0:
            time.sleep(wait)
        _last_check_started = time.time()


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
    _wait_for_turn()
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
        username, password, status = row[0].strip(), row[1], row[3].strip()
        if not (username and password):
            continue
        if status:
            continue  # already checked -- clear STATUS by hand to re-check
        with _lock:
            if i in _in_flight_rows:
                continue  # already picked up this cycle, still running
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
