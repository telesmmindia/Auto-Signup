"""
Polls the shared Google Sheet queue and drives main.run_paired_hedge (Baccarat
hedge betting) for every new row, so adding a row to the sheet is the same as
typing /pair + /run on the gameplay Telegram bot -- no manual command needed.

Sheet layout (row 1 = header, exactly as https://docs.google.com/spreadsheets
/d/14unqPI3VsjfUqhhmg666lPJBeFQu3x9VEwSLk_o05dM already has it):

    A: PLAYER 1   B: PASSWORD   C: PLAYER 2   D: PASSWORD
    E: BETS AMOUNTS   F: ROUNDS   G: STATUS

A row is picked up once A-F are all filled and STATUS is empty. STATUS is
then written live: "queued" -> "running" -> a result summary (rounds hedged,
stop reason, final balance/net for each side). Clear STATUS on a row to make
the watcher re-run it.

Setup (one-time):
    .venv/bin/pip install gspread google-auth
    Create a Google Cloud project -> enable the Google Sheets API -> create a
    service account -> download its JSON key -> share the sheet with the
    service account's email (Editor access, since STATUS gets written back).

Run:
    SHEET_CREDENTIALS_FILE=service_account.json \
        .venv/bin/python sheet_watcher.py --env .env.gameplay

Reuses .env.gameplay's BOT_SITE_URL and SETTINGS_FILE (so it automatically
picks up whatever proxy /setproxy currently has set on the gameplay bot) --
same site/game as the Telegram gameplay bot, just a different trigger.
"""
import json
import os
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor

# --env <path> selects which env file to load, same convention as
# telegram_bot.py -- lets this run against gameplay's site/proxy settings
# without duplicating them into a new env file.
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
# both files define, same gotcha documented in telegram_bot.py.
import main as engine
load_dotenv(_env_file, override=True)

SPREADSHEET_ID = os.environ.get(
    "SHEET_SPREADSHEET_ID", "14unqPI3VsjfUqhhmg666lPJBeFQu3x9VEwSLk_o05dM")
WORKSHEET_GID = os.environ.get("SHEET_WORKSHEET_GID", "0")
CREDENTIALS_FILE = os.environ.get("SHEET_CREDENTIALS_FILE", "service_account.json")
POLL_SECONDS = int(os.environ.get("SHEET_POLL_SECONDS", "20"))
MAX_CONCURRENT = int(os.environ.get("SHEET_MAX_CONCURRENT_RUNS", "2"))
SITE_URL = os.environ.get("BOT_SITE_URL") or engine.SITE_URL
SETTINGS_FILE = os.environ.get("SETTINGS_FILE", "bot_settings.json")
RUNS_LOG_FILE = os.environ.get("SHEET_RUNS_FILE", "sheet_runs.json")

COL_PLAYER1, COL_PASS1, COL_PLAYER2, COL_PASS2, COL_AMOUNT, COL_ROUNDS, COL_STATUS = range(1, 8)

_lock = threading.Lock()
_in_flight_rows = set()
_busy_usernames = set()


def current_proxy():
    """Mirrors the gameplay bot's global_settings.get("proxy") by reading the
    same SETTINGS_FILE live on each run, so /setproxy on the bot also applies
    here with no separate config."""
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


def _clean_number(raw):
    return (raw or "").replace("₹", "").replace(",", "").strip()


def _append_run_log(entry):
    try:
        data = {"runs": []}
        if os.path.exists(RUNS_LOG_FILE):
            with open(RUNS_LOG_FILE) as f:
                data = json.load(f)
        data["runs"].append(entry)
        with open(RUNS_LOG_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception:
        traceback.print_exc()


def _net_str(start, end):
    if start is None or end is None:
        return "?"
    diff = end - start
    return f"{'+' if diff >= 0 else ''}{diff}"


def format_status(summary):
    done = summary.get("rounds_done", 0)
    req = summary.get("requested_rounds", 0)
    reason = summary.get("stop_reason", "unknown")
    icon = "✅" if summary.get("ok") else "⚠️"
    sb, fb = summary.get("start_balance", {}), summary.get("final_balance", {})
    ts = time.strftime("%Y-%m-%d %H:%M")
    line = (f"{icon} {ts} · {done}/{req} rounds · {reason}\n"
            f"B {fb.get('banker', '?')} ({_net_str(sb.get('banker'), fb.get('banker'))})  "
            f"P {fb.get('player', '?')} ({_net_str(sb.get('player'), fb.get('player'))})")
    unhedged = summary.get("unhedged_rounds") or []
    if unhedged:
        line += f"\n⚠️ {len(unhedged)} one-sided landing(s), retried automatically"
    return line


def process_row(ws, row_idx, banker_user, banker_pass, player_user, player_pass, amount, rounds):
    print(f"[row {row_idx}] starting: {banker_user} (Banker) vs {player_user} (Player), "
          f"₹{amount} x {rounds} rounds")
    try:
        ws.update_cell(row_idx, COL_STATUS,
                        f"🏃 running (started {time.strftime('%Y-%m-%d %H:%M')})")
    except Exception:
        traceback.print_exc()

    summary = {"ok": False, "rounds_done": 0, "requested_rounds": rounds,
               "stop_reason": "exception", "messages": [], "final_balance": {},
               "start_balance": {}}
    try:
        summary = engine.run_paired_hedge(
            {"username": banker_user, "password": banker_pass},
            {"username": player_user, "password": player_pass},
            amount, rounds, site_url=SITE_URL,
            progress=lambda msg: print(f"[row {row_idx}] {msg}"),
            proxy=current_proxy(), game=engine.BACCARAT)
    except Exception as e:
        traceback.print_exc()
        summary["messages"].append(f"Unhandled error: {e}")

    try:
        ws.update_cell(row_idx, COL_STATUS, format_status(summary))
    except Exception:
        traceback.print_exc()

    _append_run_log({
        "row": row_idx, "banker_username": banker_user, "player_username": player_user,
        "amount": amount, "requested_rounds": rounds,
        "rounds_done": summary.get("rounds_done"), "stop_reason": summary.get("stop_reason"),
        "started_at": summary.get("started_at"), "ended_at": summary.get("ended_at"),
        "start_balance": summary.get("start_balance"), "final_balance": summary.get("final_balance"),
        "rounds": summary.get("rounds"), "unhedged_rounds": summary.get("unhedged_rounds"),
        "messages": summary.get("messages"),
    })

    with _lock:
        _in_flight_rows.discard(row_idx)
        _busy_usernames.discard(banker_user)
        _busy_usernames.discard(player_user)
    print(f"[row {row_idx}] done: {summary.get('stop_reason')}")


def poll_once(ws, executor):
    rows = ws.get_all_values()
    for i, row in enumerate(rows[1:], start=2):  # row 1 is the header
        row = row + [""] * (7 - len(row))
        p1, pw1, p2, pw2, amount_raw, rounds_raw, status = row[:7]
        if not (p1.strip() and pw1.strip() and p2.strip() and pw2.strip()
                and amount_raw.strip() and rounds_raw.strip()):
            continue
        if status.strip():
            continue  # already queued/running/finished -- clear STATUS to re-run

        with _lock:
            if i in _in_flight_rows:
                continue
            if p1.strip() in _busy_usernames or p2.strip() in _busy_usernames:
                continue

        try:
            amount = int(float(_clean_number(amount_raw)))
            rounds = int(_clean_number(rounds_raw))
        except ValueError:
            try:
                ws.update_cell(i, COL_STATUS, "❌ invalid amount/rounds")
            except Exception:
                traceback.print_exc()
            continue
        if amount <= 0 or rounds <= 0:
            try:
                ws.update_cell(i, COL_STATUS, "❌ amount/rounds must be positive")
            except Exception:
                traceback.print_exc()
            continue

        with _lock:
            _in_flight_rows.add(i)
            _busy_usernames.add(p1.strip())
            _busy_usernames.add(p2.strip())
        try:
            ws.update_cell(i, COL_STATUS, "⏳ queued")
        except Exception:
            traceback.print_exc()
        executor.submit(process_row, ws, i, p1.strip(), pw1, p2.strip(), pw2, amount, rounds)


def main():
    print(f"sheet_watcher: spreadsheet={SPREADSHEET_ID} gid={WORKSHEET_GID} "
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
        print("\nStopping (waiting for in-flight runs to finish)...")
    finally:
        executor.shutdown(wait=True)


if __name__ == "__main__":
    main()
