"""Sheet-driven knockout tournament: read a roster, play it down to one winner.

Fourth sheet-driven script in this repo, and it follows sheet_watcher.py /
balance_checker.py / password_changer.py exactly where it can: same `--env`
flag and the same load_dotenv(override=True)-after-`import main` ordering
gotcha, same current_proxy() pattern (re-reads the env file's SETTINGS_FILE on
every run, so /setproxy on the matching Telegram bot applies here too), same
service-account setup.

Where it DIFFERS from those three: they poll a queue of independent rows, each
of which is its own small job. A tournament is one event with a beginning and
an end, so the roster is not a queue -- the whole sheet is a single run. There
are no per-row STATUS semantics; the sheet is the roster going in and the
scoreboard coming out.

Sheet layout (row 1 = header):

    A: USERNAME | B: PASSWORD | C: START BALANCE | D: BALANCE | E: STAGE OUT | F: RESULT

USERNAME/PASSWORD are yours to fill. The script writes the other three as it
goes: BALANCE is that account's balance when it left the bracket, STAGE OUT is
which stage knocked it out, and RESULT is `winner`, `eliminated`, or a problem
note. Only rows with BOTH A and B filled enter.

Two ways to start a run:

  * one-shot -- run the command, type PLAY at the prompt. Good for testing.
  * --watch  -- run as a service and start runs from the sheet itself, with no
    terminal. It watches one control cell (H1 by default): type START there and
    it plays. The cell doubles as the status readout, so the sheet is the whole
    interface.

    NOTE this fires real betting across the whole roster the moment the cell
    reads START, with no second confirmation -- an earlier ARM-then-START
    handshake was removed as too fiddly. Guard it by restricting who can edit
    the sheet, not by relying on a prompt.

Real money. Start with --dry-run, which logs every account in, opens every
table, computes every stake and prints the bracket -- but never clicks a bet.

Usage:
    .venv/bin/python tournament_runner.py --env .env.tournament.cricmatch --dry-run
    .venv/bin/python tournament_runner.py --env .env.tournament.cricmatch
    .venv/bin/python tournament_runner.py --env .env.tournament.cricmatch --watch
    .venv/bin/python tournament_runner.py --env .env.tournament.cricmatch --csv roster.csv
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
# Seconds between the HTTP logins --check makes. These are cheap (~3s each,
# no browser) but they still count against the same POST /login rate rule, so
# they are paced too -- just far tighter than a browser seat needs to be.
CHECK_SPACING = float(os.environ.get("TOURNAMENT_CHECK_SPACING", "5"))
TABLE_MIN = int(os.environ.get("TOURNAMENT_TABLE_MIN", str(T.DEFAULT_TABLE_MIN)))
TABLE_MAX = int(os.environ.get("TOURNAMENT_TABLE_MAX", str(T.DEFAULT_TABLE_MAX)))

HEADER = ["USERNAME", "PASSWORD", "START BALANCE", "BALANCE", "STAGE OUT", "RESULT"]


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
    ap.add_argument("--check", action="store_true",
                    help="just ask the site about every account's login over "
                         "HTTP and print what it says -- no browsers, no bets")
    ap.add_argument("--watch", action="store_true",
                    help=f"run as a service, starting a tournament whenever "
                         f"{CONTROL_CELL} in the sheet is set to START")
    args = ap.parse_args()

    site_url = args.url or SITE_URL
    ws = None

    if args.watch:
        if args.csv:
            raise SystemExit("--watch needs the sheet (that is where the "
                             "control cell lives); drop --csv.")
        return watch_loop(args, site_url)

    if args.csv:
        roster = roster_from_csv(args.csv)
        log(f"Roster: {len(roster)} account(s) from {args.csv}")
    else:
        ws = open_worksheet()
        roster = roster_from_sheet(ws)
        log(f"Roster: {len(roster)} account(s) from sheet {SPREADSHEET_ID}")
        ensure_header(ws)

    if args.limit:
        roster = roster[:args.limit]

    if args.check:
        return check_roster(roster, site_url)

    if len(roster) < 2:
        raise SystemExit("Need at least two accounts to run a tournament.")

    if not args.dry_run and not args.yes:
        # This concentrates every entrant's balance into one account by
        # betting it. There is no undo.
        preflight(roster, site_url, args)
        reply = input("Type PLAY to start betting for real: ").strip()
        if reply != "PLAY":
            raise SystemExit("Aborted -- nothing was bet.")
        summary = play(ws, roster, site_url, args)
    else:
        preflight(roster, site_url, args)
        summary = play(ws, roster, site_url, args)
    return summary


def check_roster(roster, site_url):
    """Ask the site about every account's login over HTTP and print the answer.

    A tournament that seats nobody reports "login did not complete", which
    cannot tell a wrong password from a rate block -- and finding out the slow
    way costs ~40s of browser per account plus a real login against the very
    rate limit that may be the problem. This is the same ~3s HTTP check
    seat_accounts() uses between retries (tournament.diagnose_account), run
    over the whole roster before committing to anything. It places no bets and
    opens no browser."""
    proxies = current_proxies()
    log("")
    log(f"  site   : {site_url}")
    log(f"  proxies: {len(proxies)} "
        f"({'direct' if proxies == [None] else 'configured'})")
    log(f"  checking {len(roster)} account(s), {CHECK_SPACING}s apart")
    log("")

    counts = {}
    for i, acct in enumerate(roster):
        if i:
            time.sleep(CHECK_SPACING)
        state, bal, detail = T.diagnose_account(
            acct["username"], acct["password"], site_url,
            proxies[i % len(proxies)])
        counts[state] = counts.get(state, 0) + 1
        held = "" if bal is None else f"  balance {bal}"
        log(f"  {acct['username']:<24} {state:<9}{held:<18} {detail}")
        if state == "blocked":
            # Everything after this would be measuring the block, not the
            # accounts -- and each further attempt extends it.
            log("")
            log("  The site is rate-limiting logins (its edge answered, not "
                "the app), so nothing here was really checked. Stopping -- "
                "wait ~20 minutes and run this again.")
            return counts
    log("")
    log("  " + ", ".join(f"{n} {state}" for state, n in sorted(counts.items())))
    if counts.get("rejected"):
        log("  'rejected' means the site itself refused the username or "
            "password -- fix those rows in the sheet; retrying cannot help.")
    if counts.get("ok") == len(roster):
        log("  Every login works, so a seating failure is the table/browser "
            "path, not the accounts.")
    return counts


def preflight(roster, site_url, args):
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


def play(ws, roster, site_url, args, on_stage=None):
    """Run one tournament and write results back. Shared by the one-shot path
    and the sheet-triggered watcher."""
    proxies = current_proxies()
    by_user = {r["username"]: r for r in roster}
    pending = []

    def on_account(username, balance, result, stage, start_balance=None):
        """Buffer per-account outcomes; flushed to the sheet after each group."""
        pending.append((username, balance, stage, result, start_balance))

    def flush():
        if ws is None or not pending:
            return
        while pending:
            entry = pending.pop(0)
            username, balance, stage, result = entry[0], entry[1], entry[2], entry[3]
            start_balance = entry[4] if len(entry) > 4 else None
            row = by_user.get(username, {}).get("_row")
            if not row:
                continue
            try:
                ws.update(range_name=f"D{row}:F{row}",
                          values=[[balance if balance is not None else "",
                                   stage, result]])
            except Exception as exc:
                log(f"   (could not write row {row}: {exc})")
            # Write start balance only once (when it first appears)
            if start_balance is not None:
                try:
                    existing = ws.cell(row, 3).value  # column C = START BALANCE
                    if not existing:
                        ws.update(range_name=f"C{row}",
                                  values=[[start_balance]])
                except Exception:
                    pass

    def progress(msg):
        log(msg)
        flush()
        if on_stage and msg.startswith("=== STAGE"):
            on_stage(msg.strip("= ").strip())

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
                ws.update(range_name=f"F{row}", values=[["WINNER"]])
            except Exception:
                pass
    return summary


# ---------------------------------------------------------------------------
# Sheet-triggered mode
# ---------------------------------------------------------------------------
#
# Runs as a service and watches one cell, so a run is started by typing in the
# sheet rather than by opening a terminal.
#
# A single word starts it: typing START begins real betting across the whole
# roster immediately, with no confirmation step. This was originally a
# two-step ARM-then-START handshake, removed on request as too confusing in
# daily use. The trade is real -- a stray paste into the control cell now
# starts a tournament -- so the protection is sheet edit permissions, not the
# script. Keep the roster sheet restricted to people who should be able to
# spend the balances in it.
# ---------------------------------------------------------------------------

CONTROL_CELL = os.environ.get("TOURNAMENT_CONTROL_CELL", "I1")
CONTROL_LABEL_CELL = os.environ.get("TOURNAMENT_CONTROL_LABEL_CELL", "H1")
POLL_SECONDS = float(os.environ.get("TOURNAMENT_POLL_SECONDS", "20"))


def ensure_header(ws):
    try:
        if ws.row_values(1)[:1] != HEADER[:1]:
            ws.update(range_name="A1:F1", values=[HEADER])
    except Exception:
        pass


def read_control(ws):
    try:
        return (ws.acell(CONTROL_CELL).value or "").strip()
    except Exception as exc:
        log(f"(could not read {CONTROL_CELL}: {exc})")
        return None


def write_control(ws, text):
    try:
        ws.update(range_name=CONTROL_CELL, values=[[text]])
    except Exception as exc:
        log(f"(could not write {CONTROL_CELL}: {exc})")


def clear_results(ws, roster):
    """Blank D:F for every entrant so a new run does not show stale results."""
    if not roster:
        return
    rows = [r["_row"] for r in roster if r.get("_row")]
    if not rows:
        return
    try:
        ws.update(range_name=f"D{min(rows)}:F{max(rows)}",
                  values=[["", "", ""] for _ in range(min(rows), max(rows) + 1)])
    except Exception as exc:
        log(f"(could not clear old results: {exc})")


def watch_loop(args, site_url):
    ws = open_worksheet()
    ensure_header(ws)
    try:
        ws.update(range_name=CONTROL_LABEL_CELL, values=[["CONTROL"]])
    except Exception:
        pass

    log(f"Watching {CONTROL_CELL} every {POLL_SECONDS:.0f}s.")
    log(f"  type START in {CONTROL_CELL} to play for real.")
    if (read_control(ws) or "").upper() != "START":
        write_control(ws, "IDLE — type START to begin")

    first = True
    while True:
        try:
            # The sleep lives inside the try so Ctrl-C between polls exits
            # cleanly instead of raising through the loop.
            if not first:
                time.sleep(POLL_SECONDS)
            first = False
            cmd = read_control(ws)
            if cmd is None:
                time.sleep(POLL_SECONDS)
                continue
            upper = cmd.upper()

            if upper == "START":
                roster = roster_from_sheet(ws)
                if args.limit:
                    roster = roster[:args.limit]
                if len(roster) < 2:
                    write_control(ws, "REFUSED — need at least 2 accounts "
                                      "with a username and password")
                    continue

                started = time.strftime("%H:%M")
                write_control(ws, f"RUNNING since {started} — "
                                  f"{len(roster)} entrants")
                log(f"\n=== START: {len(roster)} entrants at {started} ===")
                clear_results(ws, roster)
                preflight(roster, site_url, args)

                def on_stage(stage_msg):
                    write_control(ws, f"RUNNING since {started} — {stage_msg}")

                try:
                    summary = play(ws, roster, site_url, args,
                                   on_stage=on_stage)
                except Exception as exc:
                    log(f"run failed: {exc}")
                    write_control(ws, f"FAILED {time.strftime('%H:%M')} — "
                                      f"{str(exc)[:120]}")
                    continue

                done = time.strftime("%H:%M")
                if summary.get("winner"):
                    write_control(
                        ws, f"DONE {done} — winner {summary['winner']} "
                            f"({summary.get('winner_balance')}). "
                            f"Type START to run again.")
                else:
                    write_control(
                        ws, f"DONE {done} — no winner, "
                            f"{len(summary['problems'])} problem(s), see "
                            f"{STATE_FILE}. Type START to run again.")

            elif upper in ("STOP", "IDLE"):
                write_control(ws, "IDLE — type START to begin")

        except KeyboardInterrupt:
            log("\nstopped")
            return
        except Exception as exc:
            log(f"(watch loop error, continuing: {exc})")


if __name__ == "__main__":
    main_()
