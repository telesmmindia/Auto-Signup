"""Sheet-driven knockout tournament: read a roster, play it down to one winner.

Fourth sheet-driven script in this repo, and it follows sheet_watcher.py /
balance_checker.py / password_changer.py exactly where it can: same `--env`
flag and the same load_dotenv(override=True)-after-`import main` ordering
gotcha, same current_proxy() pattern (re-reads the env file's SETTINGS_FILE on
every run, so /setproxy on the matching Telegram bot applies here too), same
service-account setup.

Where it deliberately DIFFERS from those three: they are pollers, looping
forever over a queue of independent rows. A tournament is a single event with
a beginning and an end, so this runs ONCE and exits. There is no poll loop and
no per-row STATUS queue semantics -- the sheet is the roster going in and the
scoreboard coming out.

Sheet layout (row 1 = header):

    A: USERNAME | B: PASSWORD | C: BALANCE | D: STAGE OUT | E: RESULT

USERNAME/PASSWORD are yours to fill. The script writes the other three as it
goes: BALANCE is that account's balance when it left the bracket, STAGE OUT is
which stage knocked it out, and RESULT is `winner`, `eliminated`, or a problem
note. Only rows with BOTH A and B filled enter.

Real money. Start with --dry-run, which logs every account in, opens every
table, computes every stake and prints the bracket -- but never clicks a bet.

Usage:
    .venv/bin/python tournament_runner.py --env .env.gameplay --dry-run
    .venv/bin/python tournament_runner.py --env .env.gameplay
    .venv/bin/python tournament_runner.py --env .env.gameplay --csv roster.csv
"""
import argparse
import csv
import json
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

import main  # noqa: F401  -- imported before load_dotenv, see below

# `import main` runs its own bare load_dotenv() as an import-time side effect,
# and python-dotenv defaults to first-load-wins. Without override=True here a
# real .env in the repo root would silently beat --env for every shared key.
_env_file = None
if "--env" in sys.argv:
    _env_file = sys.argv[sys.argv.index("--env") + 1]
if _env_file:
    load_dotenv(_env_file, override=True)
else:
    load_dotenv()

import tournament as T  # noqa: E402


SPREADSHEET_ID = os.environ.get("TOURNAMENT_SPREADSHEET_ID", "")
WORKSHEET_GID = os.environ.get("TOURNAMENT_WORKSHEET_GID", "0")
CREDENTIALS_FILE = (os.environ.get("TOURNAMENT_CREDENTIALS_FILE")
                    or os.environ.get("SHEET_CREDENTIALS_FILE")
                    or "service_account.json")
SETTINGS_FILE = os.environ.get("SETTINGS_FILE", "bot_settings.json")
SITE_URL = os.environ.get("BOT_SITE_URL") or main.SITE_URL
STATE_FILE = os.environ.get("TOURNAMENT_STATE_FILE", "tournament_state.json")

# One Chromium per seat, so this is machine capacity, not a tuning knob.
GROUP_SIZE = max(2, int(os.environ.get("TOURNAMENT_GROUP_SIZE", "10")))
# Seconds between starting one seat's login and the next. A burst of ~10
# simultaneous logins from one IP is what trips the site's bare-403 block --
# see CLAUDE.md's balance_checker findings.
LOGIN_SPACING = float(os.environ.get("TOURNAMENT_LOGIN_SPACING", "20"))
TABLE_MIN = int(os.environ.get("TOURNAMENT_TABLE_MIN", str(T.DEFAULT_TABLE_MIN)))
TABLE_MAX = int(os.environ.get("TOURNAMENT_TABLE_MAX", str(T.DEFAULT_TABLE_MAX)))

HEADER = ["USERNAME", "PASSWORD", "BALANCE", "STAGE OUT", "RESULT"]


def log(msg=""):
    print(msg, flush=True)


def current_proxies():
    """Proxy list for this run.

    TOURNAMENT_PROXIES (comma/space separated) wins if set -- spreading seats
    across several IPs is the only real defence against the login rate block.
    Otherwise fall back to the single proxy the matching bot instance has
    configured, re-read live so /setproxy applies without editing anything
    here."""
    raw = os.environ.get("TOURNAMENT_PROXIES", "").replace(",", " ").split()
    if raw:
        return raw
    try:
        with open(SETTINGS_FILE) as fh:
            proxy = (json.load(fh) or {}).get("proxy")
            if proxy:
                return [proxy]
    except Exception:
        pass
    return [None]


# ---------------------------------------------------------------------------
# Roster sources
# ---------------------------------------------------------------------------

def roster_from_csv(path):
    rows = []
    with open(path, newline="") as fh:
        for rec in csv.reader(fh):
            if len(rec) < 2:
                continue
            user, pw = rec[0].strip(), rec[1].strip()
            if not user or not pw:
                continue
            if user.upper() == "USERNAME":       # header line
                continue
            rows.append({"username": user, "password": pw})
    return rows


def open_worksheet():
    import gspread
    from google.oauth2.service_account import Credentials

    if not SPREADSHEET_ID:
        raise SystemExit(
            "TOURNAMENT_SPREADSHEET_ID is not set. Put it in your --env file "
            "(or use --csv to run from a local file instead).")
    creds = Credentials.from_service_account_file(
        CREDENTIALS_FILE,
        scopes=["https://www.googleapis.com/auth/spreadsheets"])
    book = gspread.authorize(creds).open_by_key(SPREADSHEET_ID)
    for ws in book.worksheets():
        if str(ws.id) == str(WORKSHEET_GID):
            return ws
    return book.sheet1


def roster_from_sheet(ws):
    rows = ws.get_all_values()
    roster = []
    for idx, rec in enumerate(rows, start=1):
        if idx == 1 and rec and rec[0].strip().upper() == "USERNAME":
            continue
        user = rec[0].strip() if len(rec) > 0 else ""
        pw = rec[1].strip() if len(rec) > 1 else ""
        if user and pw:
            roster.append({"username": user, "password": pw, "_row": idx})
    return roster


# ---------------------------------------------------------------------------

def main_():
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", default=None, help="env file (parsed above)")
    ap.add_argument("--csv", default=None,
                    help="run from a local CSV instead of the sheet")
    ap.add_argument("--dry-run", action="store_true",
                    help="seat everyone and compute every stake, but never "
                         "place a bet")
    ap.add_argument("--group-size", type=int, default=GROUP_SIZE,
                    help=f"browsers running at once (default {GROUP_SIZE})")
    ap.add_argument("--limit", type=int, default=None,
                    help="only enter the first N accounts")
    ap.add_argument("--url", default=None)
    ap.add_argument("--yes", action="store_true",
                    help="skip the confirmation prompt")
    args = ap.parse_args()

    site_url = args.url or SITE_URL
    ws = None

    if args.csv:
        roster = roster_from_csv(args.csv)
        log(f"Roster: {len(roster)} account(s) from {args.csv}")
    else:
        ws = open_worksheet()
        roster = roster_from_sheet(ws)
        log(f"Roster: {len(roster)} account(s) from sheet {SPREADSHEET_ID}")
        try:
            if ws.row_values(1)[:1] != HEADER[:1]:
                ws.update("A1:E1", [HEADER])
        except Exception:
            pass

    if args.limit:
        roster = roster[:args.limit]
    if len(roster) < 2:
        raise SystemExit("Need at least two accounts to run a tournament.")

    proxies = current_proxies()
    rounds = max(1, (len(roster) - 1).bit_length())

    log("")
    log(f"  site        : {site_url}")
    log(f"  entrants    : {len(roster)}")
    log(f"  group size  : {args.group_size} browsers at once")
    log(f"  proxies     : {len(proxies)} "
        f"({'direct' if proxies == [None] else 'configured'})")
    log(f"  login pacing: {LOGIN_SPACING}s between seats")
    log(f"  table limits: {TABLE_MIN} – {TABLE_MAX} per bet")
    log(f"  click budget: {T.MAX_BET_CLICKS} chips per stake")
    log(f"  ~rounds     : {rounds} for one winner")
    log(f"  mode        : {'DRY RUN (no bets)' if args.dry_run else '*** REAL MONEY ***'}")
    log("")

    if not args.dry_run and not args.yes:
        # This concentrates every entrant's balance into one account by
        # betting it. There is no undo.
        reply = input("Type PLAY to start betting for real: ").strip()
        if reply != "PLAY":
            raise SystemExit("Aborted -- nothing was bet.")

    by_user = {r["username"]: r for r in roster}
    pending = []

    def on_account(username, balance, result, stage):
        """Buffer per-account outcomes; flushed to the sheet after each group."""
        pending.append((username, balance, stage, result))

    def flush():
        if ws is None or not pending:
            return
        while pending:
            username, balance, stage, result = pending.pop(0)
            row = by_user.get(username, {}).get("_row")
            if not row:
                continue
            try:
                ws.update(f"C{row}:E{row}",
                          [[balance if balance is not None else "",
                            stage, result]])
            except Exception as exc:
                log(f"   (could not write row {row}: {exc})")

    def progress(msg):
        log(msg)
        flush()

    t0 = time.time()
    summary = T.run_tournament(
        roster, site_url=site_url, proxies=proxies,
        group_size=args.group_size, table_min=TABLE_MIN, table_max=TABLE_MAX,
        progress=progress, dry_run=args.dry_run,
        login_spacing=LOGIN_SPACING, state_path=STATE_FILE,
        on_account=on_account)
    flush()

    log("")
    log("=" * 62)
    log(f"finished in {(time.time() - t0) / 60:.1f} min")
    log(f"entrants   : {summary['entrants']}")
    log(f"stages     : {len(summary['stages'])}")
    if summary.get("winner"):
        log(f"WINNER     : {summary['winner']} "
            f"(balance {summary.get('winner_balance')})")
    elif args.dry_run:
        log("WINNER     : n/a -- dry run, nothing was bet")
    else:
        log("WINNER     : none -- see problems below")
    if summary["problems"]:
        log("")
        log(f"problems ({len(summary['problems'])}) -- these need a human:")
        for p in summary["problems"]:
            log(f"  * {p}")
    log(f"full record: {STATE_FILE}")

    if ws is not None and summary.get("winner"):
        row = by_user.get(summary["winner"], {}).get("_row")
        if row:
            try:
                ws.update(f"E{row}", [["WINNER"]])
            except Exception:
                pass


if __name__ == "__main__":
    main_()
