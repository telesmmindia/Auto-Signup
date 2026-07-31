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
a WAF block doesn't blank out the last known-good value.

A REAL application result (✅ success, or a ❌ genuine rejection like wrong
password / "Account has been Blocked") is terminal, same as above -- clear
STATUS by hand to retry. An INFRA-level block (a bare 403/non-JSON response
from the edge/WAF, before the site ever saw the request -- see
main.py's infra_block flag) is NOT treated as a real result: it's written as
a "⏳ rate-limited, auto-retrying" marker and poll_once() keeps picking that
row back up automatically, no manual clear needed -- see _trip_circuit_breaker()
below for why this matters at scale.

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

### Speeding this up: BALANCE_CHECK_PROXIES (multiple proxy IPs)

Everything above caps throughput at roughly one check per CHECK_SPACING_SECONDS
(~2/min) because it all goes through ONE proxy IP, and that IP is what the
site's rate limit is actually tracking -- pacing/backoff make a single IP
reliable, not fast. The only real way to raise throughput is to spread
checks across multiple IPs, each staying under the same safe per-IP rate.

Set BALANCE_CHECK_PROXIES to a comma- or newline-separated list of proxy
strings (same format as --proxy/parse_proxy) to enable this:

    BALANCE_CHECK_PROXIES="host1:port1:user1:pass1,host2:port2:user2:pass2" \\
        .venv/bin/python balance_checker.py --env .env.cricmatch

Each proxy in the list gets its OWN independent CHECK_SPACING_SECONDS pacing
lane and its OWN circuit breaker -- one proxy tripping a block pauses only
that proxy's lane, not the others, since the block is a property of that IP,
not of this script. Rows are handed out round-robin across the pool
(_next_proxy()) as they're picked up, and MAX_CONCURRENT auto-scales to the
pool size (one worker per proxy) unless BALANCE_MAX_CONCURRENT overrides it.
N proxies therefore gives close to N times the throughput of one, with no
extra block risk per IP.

When BALANCE_CHECK_PROXIES is unset, behavior is unchanged from before this
feature: a single global proxy (from the bot's SETTINGS_FILE, i.e. whatever
/setproxy currently has set) shared by every check under one pacing/backoff
lane, same as always.

**Prefer RESIDENTIAL proxies, same as everywhere else in this codebase** --
CLAUDE.md's proxy section documents datacenter IPs getting outright
WAF-blocked by IP reputation on this site, a completely different (and
worse) failure mode than the rate limit this feature works around. Getting
more datacenter IPs would not fix anything here.

Not yet run live with more than one proxy -- built and unit-tested with
mocked proxies/checker (confirmed round-robin assignment and independent
per-proxy backoff), but no real second proxy has been exercised end-to-end
yet. Try `/testproxy` (bot) or a one-off `http_check_account_balance()` call
against each new proxy first to confirm it actually works against this site
before adding it to the list.
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

# See "Speeding this up: BALANCE_CHECK_PROXIES" in the module docstring.
# Unset -> the single global proxy from SETTINGS_FILE, one pacing/backoff
# lane, same as before this feature existed. Set -> a pool of proxy strings,
# each with its OWN lane, handed out round-robin per row.
_CHECK_PROXIES_RAW = os.environ.get("BALANCE_CHECK_PROXIES", "").strip()
CHECK_PROXIES = ([p.strip() for p in _CHECK_PROXIES_RAW.replace("\n", ",").split(",") if p.strip()]
                 if _CHECK_PROXIES_RAW else None)

# Defaults to 1 lane's worth of concurrency (one proxy = one IP = must stay
# serialized on that IP, per the note below) unless a proxy pool is set, in
# which case one worker per proxy lets them all run in parallel -- still
# only ever ONE in-flight request per individual proxy at a time, since each
# proxy's own _wait_for_turn() lock enforces that regardless of how many
# workers the executor has.
MAX_CONCURRENT = int(os.environ.get("BALANCE_MAX_CONCURRENT", str(len(CHECK_PROXIES) if CHECK_PROXIES else "1")))
# Still defaults to 1 lane, not higher, when no proxy pool is set: every
# account then shares one proxy IP, and several NEW rows added at once would
# otherwise fire that many concurrent login POSTs from the same IP in the
# same instant -- confirmed live 2026-07-30 that concurrent logins from one
# IP (not just rapid sequential ones) trip cricmatch247's edge-level rate
# block (a bare 403 on /login, same category documented in CLAUDE.md for
# /register and /send_otp_touser). A serialized queue of new rows still
# clears in ~3s each, so there's no real throughput reason to raise this for
# a single proxy -- add more proxies to BALANCE_CHECK_PROXIES instead.
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

# Real 2026-07-31 sheet data: once cricmatch247's edge block trips, EVERY
# attempt 403s for ~20 minutes straight no matter how paced the retries are
# -- CHECK_SPACING_SECONDS alone doesn't stop it from tripping, and kept
# spending a request every 30s into a dead window for the whole 20 minutes.
# BLOCK_BACKOFF_SECONDS is a circuit breaker: the first infra-level failure
# (bare 403/non-JSON, see main.py's infra_block flag) pauses ALL checks for
# this long, then probes once; if still blocked, the probe extends the pause
# by another BLOCK_BACKOFF_SECONDS rather than resuming full-speed retries.
# This covers a real ~20min block in ~4 wasted probes instead of ~40 wasted
# full attempts. Not a controlled-tested value -- 5 min is a starting guess.
BLOCK_BACKOFF_SECONDS = int(os.environ.get("BALANCE_BLOCK_BACKOFF_SECONDS", "300"))

_lock = threading.Lock()
_in_flight_rows = set()

# Per-proxy-lane pacing/circuit-breaker state, keyed by a lane id (the proxy
# string itself when a pool is set, or the constant _DEFAULT_LANE otherwise).
# One lane == one IP == its own independent "when did we last start a check"
# / "are we currently backed off" state, so a block on one proxy never pauses
# any other proxy's lane.
_DEFAULT_LANE = "__default__"
_lanes_lock = threading.Lock()
_lanes = {}  # lane_id -> {"lock": Lock(), "last_check_started": 0.0, "blocked_until": 0.0}

_rr_lock = threading.Lock()
_next_proxy_idx = 0


def _lane_for(lane_id):
    with _lanes_lock:
        lane = _lanes.get(lane_id)
        if lane is None:
            lane = {"lock": threading.Lock(), "last_check_started": 0.0, "blocked_until": 0.0}
            _lanes[lane_id] = lane
        return lane


def _wait_for_turn(lane_id):
    """Blocks until at least CHECK_SPACING_SECONDS has passed since this
    LANE's last check STARTED (not finished) -- called from the worker
    thread(s) actually running process_row, so it paces logins without
    blocking poll_once() itself from noticing new rows. Each proxy lane has
    its own lock, so pacing on one proxy never blocks another proxy's lane."""
    lane = _lane_for(lane_id)
    with lane["lock"]:
        wait = CHECK_SPACING_SECONDS - (time.time() - lane["last_check_started"])
        if wait > 0:
            time.sleep(wait)
        lane["last_check_started"] = time.time()


def _still_in_backoff(lane_id):
    return time.time() < _lane_for(lane_id)["blocked_until"]


def _trip_circuit_breaker(lane_id):
    """Called on an infra_block result -- pushes this LANE's backoff window
    forward from NOW, so repeated blocked probes on the same proxy keep
    extending the pause instead of each one resuming a fresh full-speed
    retry loop. Other proxy lanes are unaffected."""
    lane = _lane_for(lane_id)
    lane["blocked_until"] = time.time() + BLOCK_BACKOFF_SECONDS


def current_proxy():
    """Mirrors sheet_watcher.py's current_proxy() -- reads the bot's own
    SETTINGS_FILE live on each check, so /setproxy on that Telegram bot
    applies here with no separate config. Only used when BALANCE_CHECK_PROXIES
    is NOT set (the single-proxy, pre-pool behavior)."""
    try:
        with open(SETTINGS_FILE) as f:
            return json.load(f).get("proxy")
    except Exception:
        return None


def _next_proxy():
    """Returns (proxy_string_or_None, lane_id) for the next row to check.
    Without a pool, always the single global proxy under the one default
    lane (unchanged behavior). With a pool set, round-robins through it --
    each proxy string doubles as its own lane id, so lane state and proxy
    are always the same pairing."""
    if not CHECK_PROXIES:
        return current_proxy(), _DEFAULT_LANE
    global _next_proxy_idx
    with _rr_lock:
        idx = _next_proxy_idx % len(CHECK_PROXIES)
        _next_proxy_idx += 1
    proxy = CHECK_PROXIES[idx]
    return proxy, proxy


def get_worksheet():
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=scopes)
    client = gspread.authorize(creds)
    sh = client.open_by_key(SPREADSHEET_ID)
    for ws in sh.worksheets():
        if str(ws.id) == str(WORKSHEET_GID):
            return ws
    return sh.sheet1


def process_row(ws, row_idx, username, password, proxy, lane_id):
    if _still_in_backoff(lane_id):
        # This proxy lane is in a circuit-breaker cooldown from an earlier
        # infra_block on THIS proxy -- skip this attempt entirely (no network
        # call, no spacing wait spent) and leave STATUS untouched so
        # poll_once() picks the row up again (on this or another lane) once
        # the cooldown clears. Other lanes are unaffected.
        with _lock:
            _in_flight_rows.discard(row_idx)
        return

    _wait_for_turn(lane_id)
    print(f"[row {row_idx}] checking {username} (lane={lane_id if lane_id == _DEFAULT_LANE else 'proxy'})...")
    result = {"ok": False, "balance": None, "messages": [], "shot": None, "infra_block": False}
    prof = engine.profile_for(SITE_URL)
    checker = engine.http_check_account_balance if prof.supports_http_login else engine.run_balance_check
    try:
        result = checker(username, password, site_url=SITE_URL, proxy=proxy)
    except Exception as e:
        traceback.print_exc()
        result["messages"] = [f"Unhandled error: {e}"]

    ts = time.strftime("%Y-%m-%d %H:%M")
    try:
        if result["ok"]:
            ws.update_cell(row_idx, COL_BALANCE, result["balance"])
            ws.update_cell(row_idx, COL_STATUS, f"✅ checked {ts}")
        elif result.get("infra_block"):
            # Not a real result for this account -- the edge/WAF blocked the
            # request before the site ever saw it. Trip the breaker for THIS
            # lane only (other proxies keep running), and mark this row with
            # a RETRYABLE (not terminal) status: poll_once() picks it back up
            # automatically once the cooldown clears, no manual STATUS clear
            # needed.
            _trip_circuit_breaker(lane_id)
            msg = "; ".join(result.get("messages") or ["blocked"])[:150]
            ws.update_cell(row_idx, COL_STATUS,
                            f"⏳ {ts} rate-limited, auto-retrying — {msg}")
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
        # Empty STATUS (never checked) or a "⏳ rate-limited" marker (an
        # earlier attempt hit the circuit breaker, not a real result) are
        # both eligible for pickup. Any other non-empty STATUS (✅ or a real
        # ❌ application result) is terminal -- clear it by hand to re-check.
        if status and not status.startswith("⏳"):
            continue
        with _lock:
            if i in _in_flight_rows:
                continue  # already picked up this cycle, still running
            _in_flight_rows.add(i)
        proxy, lane_id = _next_proxy()
        executor.submit(process_row, ws, i, username, password, proxy, lane_id)


def main():
    if not SPREADSHEET_ID:
        print("BALANCE_SHEET_SPREADSHEET_ID is not set -- nothing to poll. "
              "Set it to the sheet's id (from its URL) and try again.")
        sys.exit(1)
    pool_desc = f"{len(CHECK_PROXIES)} proxies (pooled)" if CHECK_PROXIES else "1 (global proxy)"
    print(f"balance_checker: spreadsheet={SPREADSHEET_ID} gid={WORKSHEET_GID} "
          f"site={SITE_URL} poll={POLL_SECONDS}s max_concurrent={MAX_CONCURRENT} proxies={pool_desc}")
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
