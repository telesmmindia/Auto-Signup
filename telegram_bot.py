"""
Telegram bot wrapper for the QA signup driver.

Two roles:
    master admin -> one or more, set via MASTER_ADMIN_ID in .env (comma- or
                    space-separated for several). Can do everything:
                    create/remove admins, set the GLOBAL proxy
                    and site URL (applies to every admin's signups), and
                    view/export all stored account data.
    admin        -> authorized by the master admin (/addadmin). Can only
                    run /newacc and /cancel -- no proxy/URL/admin/data
                    commands.
Anyone else gets an "unauthorized" reply showing their own Telegram user ID
so they can ask the master admin to add them. Telegram's native "/" command
menu is scoped per user (BotCommandScopeChat) so each role only ever *sees*
the commands it can actually run -- a random user's menu is empty.

Flow (continuous -- keeps going until /done or /cancel):
    /newacc  -> bot generates a random test identity, asks for a phone number
    (you send the phone number)
    -> bot fills + submits the signup form, asks for the OTP sent by SMS
    (you send the OTP)
    -> on success: just "Signup successful! (#id)" (no photo) plus the
       details as a one-row CSV file; on failure: the result screenshot,
       captioned with the details, plus that same CSV
    -> a NEW signup starts automatically right away (fresh identity, same
       "Send the phone number" prompt) -- only /newacc once per session,
       not once per account
    /done    -> stop after the signup currently in progress finishes (does
                not abort it; /cancel does that instead)

Master-only commands:
    /stats            -> counts of signups by status and by btag
    /stats <btag>     -> status breakdown for just that btag
    /list [N]         -> most recent N stored accounts (default 10)
    /photo <id>       -> resend a stored account's screenshot with its
                         details as the caption (id from /list)
    /export [N] [status] [url] -> export as CSV; defaults to SUCCESSFUL
                         signups only. /export all for every status,
                         /export failed for a specific one, /export 50 for
                         a row limit, /export https://example.com to filter
                         to signups made against that site URL -- all
                         combinable, in any order (master only)
    /setpassword <pw> -> fixed password for every future signup;
                         /setpassword --random reverts to a random one
    /password         -> show the current password mode
    /fast on|off      -> toggle HTTP-fast signup mode (no browser at all,
                         cricmatch247 only, ~10-20x faster; falls back to the
                         browser automatically for sites that don't support
                         it -- see CLAUDE.md)
    /fast             -> show the current fast-mode state
    /setproxy <proxy> -> set the GLOBAL proxy for every admin's signups
                         (host:port, host:port:username:password, or a URL)
    /proxy            -> show the global proxy
    /clearproxy       -> clear the global proxy (direct connection)
    /testproxy [proxy] -> open the proxy in a browser context and report the
                         exit IP; tests the global proxy if none is given
    /seturl <url>     -> set the GLOBAL site URL for every admin's signups
    /url              -> show the global site URL
    /clearurl         -> reset to the default site URL
    /btag <code>      -> set just the btag query param on the global site
                         URL, keeping its scheme/host/path
    /btag             -> show the current btag
    /addadmin <id>    -> authorize a new admin
    /removeadmin <id> -> revoke an admin
    /admins           -> list current admins

Everyone (master + admins):
    /cancel           -> abandon an in-progress signup (also stops looping)

Setup:
    cp .env.example .env
    # edit .env: TELEGRAM_BOT_TOKEN from @BotFather, and MASTER_ADMIN_ID
    # (your own Telegram user ID -- message @userinfobot to get it)
    .venv/bin/python telegram_bot.py

One Chromium instance is launched when the bot starts and reused for every
signup -- each /newacc just opens a fresh, isolated browser context (like an
incognito window) instead of paying Chromium's ~1-3s process-launch cost on
every conversation. All Playwright calls run on one dedicated worker thread
(required by Playwright's sync API), so concurrent /newacc flows from
different chats are serialized rather than run in parallel; fine for a
personal QA bot, but worth knowing if several people use it at once.

Running one bot per site:
    This same script can run as two independent processes, one per site, so
    signups for different sites don't serialize on the same worker thread and
    each site gets its own bot identity/token in Telegram. Point each process
    at its own env file with --env, e.g.:

        cp .env.example .env.cricmatch
        cp .env.example .env.spin24star
        # edit each: a DIFFERENT TELEGRAM_BOT_TOKEN (from a second @BotFather
        # bot), BOT_SITE_URL for that site, and distinct ADMINS_FILE /
        # SETTINGS_FILE paths so the two processes never write the same file
        .venv/bin/python telegram_bot.py --env .env.cricmatch
        .venv/bin/python telegram_bot.py --env .env.spin24star   # separate terminal/tmux pane

    MASTER_ADMIN_ID can be the same value in both files (one operator running
    both bots); admin_ids are still separate files per instance, just seed
    them with the same IDs via /addadmin on each bot if you want the same
    people able to run both. accounts.db is shared (its url/referral_code
    columns already distinguish rows by site), so /list, /stats, and /export
    give you combined history across both bots by default.
"""
import asyncio
import functools
import html
import json
import logging
import os
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import requests
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, Error as PWError
from telegram import BotCommand, BotCommandScopeChat, BotCommandScopeDefault, Update
from telegram.error import NetworkError, TimedOut
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

import db
from sites import profile_for
from main import (
    SHOTS_DIR, SITE_URL,
    capsolver_key, check_phone_taken, click_first_visible, extract_referral_code,
    fill_register_form, gen_account, http_fetch_csrf, http_is_error,
    http_is_phone_taken, http_register_call, http_session_for, is_waf_captcha,
    maybe_bridge_proxy, open_signup_modal, parse_proxy, read_result, run_paired_hedge,
    stop_bridge, submit_register, test_baccarat, wait_for_otp_outcome,
    wait_for_register_outcome,
)
from sites.games import BACCARAT, STOCKMARKET

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)
# httpx logs every Telegram API round-trip (getUpdates/sendMessage) at INFO,
# which buries the bot's own activity lines; keep only its warnings/errors.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

# --env <path> selects which env file to load, so the same script can run as
# two (or more) independent bot processes -- one per site -- each with its own
# token/admins/settings. Defaults to ".env" for the single-bot case.
_env_file = ".env"
if "--env" in sys.argv:
    _idx = sys.argv.index("--env")
    if _idx + 1 < len(sys.argv):
        _env_file = sys.argv[_idx + 1]
# override=True: main.py (imported above) already ran its own bare
# load_dotenv() as an import-time side effect, which would otherwise win over
# an explicit --env file for any key both files define (dotenv's default is
# override=False, i.e. first load wins).
load_dotenv(_env_file, override=True)
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
# The fixed top-level role(s). Set via .env, not changeable from the bot
# itself -- a compromised admin session should never be able to promote itself
# to master. MASTER_ADMIN_ID accepts one id or several, comma- or
# space-separated (multiple master admins); a single id behaves as before.
MASTER_ADMIN_IDS = set(
    os.environ.get("MASTER_ADMIN_ID", "").replace(",", " ").split()
)
# This instance's home site -- falls back to main.SITE_URL if unset, so a
# single-bot setup with a plain ".env" behaves exactly as before. Every
# fallback that used to read the bare SITE_URL import now reads this instead,
# so /clearurl and the default context.goto() target THIS bot's own site, not
# necessarily main.py's cricmatch247 default.
BOT_SITE_URL = os.environ.get("BOT_SITE_URL") or SITE_URL
# Which command set this instance exposes, so signup and casino/gameplay can
# run as SEPARATE bots (each with its own token) instead of one bot doing
# both. "signup" = signup flow + account data + settings; "gameplay" = the
# casino commands (/testbaccarat, /pair, /run, ...); "all" (the default)
# keeps the old everything-in-one-bot behavior. Unregistered commands simply
# don't exist on that instance -- Telegram replies nothing for them -- and
# the "/" menus only show what the instance actually has.
BOT_MODE = (os.environ.get("BOT_MODE") or "all").strip().lower()
if BOT_MODE not in ("all", "signup", "gameplay", "stockmarket"):
    raise SystemExit(
        f"BOT_MODE must be 'signup', 'gameplay', 'stockmarket', or 'all' (got {BOT_MODE!r})")
SIGNUP_ENABLED = BOT_MODE in ("all", "signup")
# "gameplay" = the original Evolution Baccarat hedge; "stockmarket" = the same
# pair/run commands driving Evolution Stock Market Live (UP vs DOWN) instead.
# They are separate modes rather than a flag on /run because the game is fixed
# per instance, exactly like BOT_SITE_URL fixes the site -- so /run needs no
# extra argument and run_cmd stays a single implementation.
GAMEPLAY_ENABLED = BOT_MODE in ("all", "gameplay")
STOCKMARKET_ENABLED = BOT_MODE == "stockmarket"
HEDGE_ENABLED = GAMEPLAY_ENABLED or STOCKMARKET_ENABLED
RUN_GAME = STOCKMARKET if STOCKMARKET_ENABLED else BACCARAT
# How many signups this bot instance can run at once. Each slot is its own
# Chromium process + dedicated worker thread (see the worker-pool comment
# below) -- raising this increases real request concurrency against the
# target site, so it should track how many *distinct* proxies/IPs are
# actually available, not just "more is faster". Defaults to 1 (old
# single-worker behavior) if unset.
BOT_CONCURRENCY = max(1, int(os.environ.get("BOT_CONCURRENCY", "1")))

# Shared across handlers; all handler coroutines run on the same asyncio event
# loop thread, so one sqlite3 connection is safe to reuse.
conn = db.get_connection()

# chat_id -> {sub_id: Session}, for signups currently in progress. A chat can
# run several signups at once (see /newacc <n>) -- each gets its own sub_id
# (1, 2, 3, ...) so replies can be routed to the right one via a leading
# "<sub_id> " prefix. When only one sub_id is active in a chat, the prefix is
# optional (a bare phone/OTP routes to that one session).
sessions = {}
# (chat_id, sub_id) pairs currently in continuous mode: that lane
# auto-restarts a fresh signup after each one finishes, until /done or
# /cancel removes it from this set.
looping_chats = set()
# Hard ceiling on /newacc <n> so a fat-fingered count can't spawn an
# unreasonable number of Chromium contexts at once.
MAX_PARALLEL_NEWACC = 10

# Paired-hedge run state. Each /run now drives two fully independent,
# temporary browsers of its own (see run_paired_hedge in main.py) rather than
# sharing slot 0 -- so several /run calls for DIFFERENT pairs can execute
# truly in parallel, each on its own worker thread in _run_executor, without
# fighting over a browser or a thread. _active_runs maps pair_id (str) ->
# that run's threading.Event, set by /stoprun to ask it to stop after the
# current round. A pair already present in _active_runs refuses a second
# concurrent /run for the SAME pair -- betting the same two accounts from two
# contexts at once would corrupt both runs' hedge and isn't a "more
# parallelism" case, it's a conflict.
_active_runs = {}  # pair_id (str) -> threading.Event
# Hard ceiling on how many /run calls can be in flight at once. Real limits in
# practice come from proxy/IP diversity and box resources (2 browsers per
# run), so this just guards against an accidental pile-up; raise via env if
# you have the proxies/hardware for more.
MAX_CONCURRENT_RUNS = max(1, int(os.environ.get("MAX_CONCURRENT_RUNS", "3")))
_run_executor = ThreadPoolExecutor(max_workers=MAX_CONCURRENT_RUNS)

# Telegram user IDs (str) the master admin has authorized, persisted across
# restarts. Gitignored -- this is access-control state, not project content.
# Overridable via ADMINS_FILE in .env so two bot processes running out of the
# same directory (one per site) don't clobber each other's admin list on
# every write -- each instance gets its own file, even if seeded with the
# same IDs to keep admin access "shared" in practice.
ADMINS_FILE = Path(os.environ.get("ADMINS_FILE", "admins.json"))
# Proxy/URL are GLOBAL PER BOT INSTANCE (master-controlled, apply to every
# admin's signups on this bot), not per-chat -- {"proxy": "...", "url": "..."}.
# Overridable via SETTINGS_FILE for the same reason as ADMINS_FILE above.
SETTINGS_FILE = Path(os.environ.get("SETTINGS_FILE", "bot_settings.json"))
# Account "pairs" for hedge betting (see /pair, /run). Overridable per bot
# instance for the same reason as ADMINS_FILE/SETTINGS_FILE above. Stores
# plaintext passwords, so it is gitignored.
PAIRS_FILE = Path(os.environ.get("PAIRS_FILE", "pairs.json"))
# Per-run hedge history so a pair's past runs can be reviewed later via /runs.
# Holds account usernames + balances (no passwords), so it's gitignored like
# pairs.json all the same; give each bot instance its own via PAIR_RUNS_FILE.
PAIR_RUNS_FILE = Path(os.environ.get("PAIR_RUNS_FILE", "pair_runs.json"))


def _load_json(path, default):
    if path.exists():
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            return default
    return default


def _save_json(path, data):
    path.write_text(json.dumps(data))


admin_ids = set(_load_json(ADMINS_FILE, []))
global_settings = _load_json(SETTINGS_FILE, {})
# {"next_id": int, "pairs": {"<id>": {"banker": {"username","password"},
#  "player": {"username","password"}, "created_at": iso}}}. acc1 -> banker,
# acc2 -> player (fixed, so a pair always bets the same side per account).
pairs = _load_json(PAIRS_FILE, {"next_id": 1, "pairs": {}})
# {"next_id": int, "runs": [ {run record, one per /run, see run_cmd} ]}.
pair_runs = _load_json(PAIR_RUNS_FILE, {"next_id": 1, "runs": []})


def save_admin_ids():
    _save_json(ADMINS_FILE, sorted(admin_ids))


def save_settings():
    _save_json(SETTINGS_FILE, global_settings)


def save_pairs():
    _save_json(PAIRS_FILE, pairs)


def save_pair_runs():
    _save_json(PAIR_RUNS_FILE, pair_runs)


def is_master(user_id):
    return str(user_id) in MASTER_ADMIN_IDS


def is_admin(user_id):
    return is_master(user_id) or str(user_id) in admin_ids


def require_role(check):
    """Decorator gating a handler to users satisfying `check(user_id)`. On
    denial, replies with the user's own Telegram ID so they can hand it to
    the master admin for /addadmin."""
    def decorator(func):
        @functools.wraps(func)
        async def wrapper(update, context):
            # Telegram delivers some updates with no user attached (channel
            # posts, my_chat_member changes, etc.) -- ignore those rather than
            # crashing on update.effective_user.id being None.
            if update.effective_user is None:
                return
            user_id = update.effective_user.id
            if not check(user_id):
                await update.message.reply_text(
                    "You are not authorized to use this bot.\n"
                    f"Your Telegram user ID: {user_id}\n"
                    "Share this with the master admin to request access."
                )
                return
            return await func(update, context)
        return wrapper
    return decorator


# Native "/" command menu, scoped per user via BotCommandScopeChat -- everyone
# else gets the empty BotCommandScopeDefault set in post_init(), so a random
# user sees no suggested commands at all (they can still type one manually
# and get the require_role() rejection above).
ADMIN_COMMANDS = []
if SIGNUP_ENABLED:
    ADMIN_COMMANDS += [
        BotCommand("newacc", "Start continuous test signups"),
        BotCommand("done", "Stop continuous signups after the current one"),
        BotCommand("cancel", "Abandon an in-progress signup"),
    ]
ADMIN_COMMANDS.append(BotCommand("start", "Show available commands"))
MASTER_COMMANDS = list(ADMIN_COMMANDS)
if SIGNUP_ENABLED:
    MASTER_COMMANDS += [
        BotCommand("list", "Recent stored accounts"),
        BotCommand("photo", "Resend a stored account's screenshot"),
        BotCommand("export", "Export accounts as a CSV file"),
        BotCommand("stats", "Counts of signups by status and btag"),
        BotCommand("setpassword", "Set a fixed password for all signups, or --random"),
        BotCommand("password", "Show the current password mode"),
        BotCommand("fast", "Toggle HTTP-fast signup mode (no browser, cricmatch only)"),
    ]
if GAMEPLAY_ENABLED:
    MASTER_COMMANDS.append(
        BotCommand("testbaccarat", "Login + place a real Baccarat bet (smoke test)"))
if HEDGE_ENABLED:
    MASTER_COMMANDS += [
        BotCommand("pair", "Create an account pair for hedge betting"),
        BotCommand("pairs", "List stored account pairs"),
        BotCommand("delpair", "Delete a stored pair"),
        BotCommand("run", f"Run a paired hedge: acc1 {RUN_GAME.side_a_label} "
                          f"vs acc2 {RUN_GAME.side_b_label}"),
        BotCommand("stoprun", "Stop the active hedge run"),
        BotCommand("runs", "List past hedge runs (optionally by pair id)"),
        BotCommand("runlog", "Per-round detail of one past run"),
    ]
# Proxy commands apply to both modes (hedge runs route through the global
# proxy too); URL/btag only affect signups (gameplay always uses BOT_SITE_URL).
MASTER_COMMANDS += [
    BotCommand("setproxy", "Set the global proxy"),
    BotCommand("proxy", "Show the global proxy"),
    BotCommand("clearproxy", "Clear the global proxy"),
    BotCommand("testproxy", "Check a proxy actually works"),
]
if SIGNUP_ENABLED:
    MASTER_COMMANDS += [
        BotCommand("seturl", "Set the global site URL for all signups"),
        BotCommand("url", "Show the global site URL"),
        BotCommand("clearurl", "Reset to the default site URL"),
        BotCommand("btag", "Set/show just the btag on the global site URL"),
    ]
MASTER_COMMANDS += [
    BotCommand("addadmin", "Authorize a new admin"),
    BotCommand("removeadmin", "Revoke an admin"),
    BotCommand("admins", "List current admins"),
]


async def post_init(application):
    """Runs once at startup, before polling begins: empties the default
    command menu, then gives the master and every already-authorized admin
    their role-appropriate menu."""
    bot = application.bot
    try:
        await bot.set_my_commands([], scope=BotCommandScopeDefault())
    except Exception as e:
        logger.warning(f"Could not clear default command menu: {e}")
    for uid in MASTER_ADMIN_IDS:
        try:
            await bot.set_my_commands(MASTER_COMMANDS,
                                      scope=BotCommandScopeChat(chat_id=int(uid)))
        except Exception as e:
            logger.warning(f"Could not set master command menu for {uid}: {e}")
    for uid in admin_ids:
        try:
            await bot.set_my_commands(ADMIN_COMMANDS, scope=BotCommandScopeChat(chat_id=int(uid)))
        except Exception as e:
            logger.warning(f"Could not set command menu for admin {uid}: {e}")


def mask_proxy_display(proxy_str):
    """Host:port (+username) for display, without echoing the password back."""
    try:
        conf = parse_proxy(proxy_str)
    except ValueError:
        return proxy_str
    if not conf:
        return "(none)"
    text = conf["server"]
    if conf.get("username"):
        text += f" (user: {conf['username']}, password hidden)"
    return text


def build_caption(row_dict):
    """Account details formatted as a Telegram photo caption (1024-char cap)."""
    lines = [f"#{row_dict['id']} [{row_dict['status']}]" if row_dict.get("id") else f"[{row_dict['status']}]"]
    lines.append(f"Username: {row_dict['username']}")
    lines.append(f"Email: {row_dict['email']}")
    lines.append(f"Password: {row_dict['password']}")
    lines.append(f"Phone: {row_dict.get('phone', '')}")
    if row_dict.get("proxy"):
        lines.append(f"Proxy: {mask_proxy_display(row_dict['proxy'])}")
    if row_dict.get("url"):
        lines.append(f"URL: {row_dict['url']}")
    if row_dict.get("notes"):
        lines.append(f"Notes: {row_dict['notes']}")
    return "\n".join(lines)[:1024]


async def send_result_photo(update, shot_path, caption):
    """Send the screenshot as a photo with account details as the caption,
    falling back to plain text if the file is missing."""
    if shot_path and Path(shot_path).exists():
        with open(shot_path, "rb") as f:
            await update.message.reply_photo(photo=f, caption=caption)
    else:
        await update.message.reply_text(caption)


async def send_csv(update, filename, row_id=None, limit=None, status=None, url=None, caption=None):
    """Export account row(s) to a CSV file and send it as a document, then
    clean up the temp file. Signup details are delivered this way (a real
    file) rather than as a text message."""
    with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        count = db.export_csv(conn, tmp_path, row_id=row_id, limit=limit, status=status, url=url)
        if count == 0:
            await update.message.reply_text("No accounts to export.")
            return
        with open(tmp_path, "rb") as f:
            await update.message.reply_document(document=f, filename=filename, caption=caption)
    finally:
        Path(tmp_path).unlink(missing_ok=True)


# BOT_CONCURRENCY independent (executor, Chromium) slots rather than one
# shared browser. Playwright's sync API is thread-affine -- every call for a
# given browser/context/page must run on the OS thread that created it -- so
# "N-way parallel" means N separate ThreadPoolExecutor(max_workers=1) pools,
# each pinned to its own Chromium process, NOT one bigger pool sharing a
# single browser. A Session picks one slot index (round-robin, see
# _next_slot()) in begin_signup() and must route every Playwright call for
# its lifetime through that same slot's executor/browser -- mixing slots for
# the same session would violate the thread-affinity requirement.
_pw_executors = [ThreadPoolExecutor(max_workers=1) for _ in range(BOT_CONCURRENCY)]
_playwrights = [None] * BOT_CONCURRENCY
_browsers = [None] * BOT_CONCURRENCY
_slot_counter = 0


def _next_slot():
    global _slot_counter
    slot = _slot_counter % BOT_CONCURRENCY
    _slot_counter += 1
    return slot


class Session:
    def __init__(self):
        self.context = None
        self.page = None
        self.acct = None
        self.row_id = None
        self.stage = None  # "await_phone" | "await_otp"
        self.proxy = None  # raw proxy string, or None for a direct connection
        self.bridge_proc = None  # local pproxy process, if the proxy needed one
        self.site_url = None  # site URL for this signup (falls back to BOT_SITE_URL)
        self.slot = 0  # index into _pw_executors/_browsers, fixed for this session's life
        self.sub_id = 1  # this chat's lane number, for routing replies when >1 lane is active
        # HTTP-fast mode (global_settings["fast"], see /fast) -- set once in
        # begin_signup() based on whether this signup's site supports it.
        # When True, handle_message() routes through _blocking_http_register()/
        # _blocking_http_verify_otp() instead of the Playwright path, and
        # context/page/slot above are never touched. http_session/http_csrf
        # carry state between the register call and the OTP-verify call (the
        # requests.Session's cookies + the CSRF token), same idea as
        # context/page for the browser path.
        self.use_fast = False
        self.http_session = None
        self.http_csrf = None


def _valid_phone(text):
    return text.isdigit() and 7 <= len(text) <= 15


def _blocking_ensure_browser(slot):
    """Launch this slot's Chromium instance once; reused by every session
    routed to this slot. Must run on _pw_executors[slot]'s worker thread."""
    if _browsers[slot] is None:
        _playwrights[slot] = sync_playwright().start()
        _browsers[slot] = _playwrights[slot].chromium.launch(headless=True)
    return _browsers[slot]


def _blocking_shutdown_browser(slot):
    try:
        if _browsers[slot]:
            _browsers[slot].close()
    except Exception:
        pass
    try:
        if _playwrights[slot]:
            _playwrights[slot].stop()
    except Exception:
        pass
    _browsers[slot] = None
    _playwrights[slot] = None


def _blocking_close_context(session):
    """Must run on session.slot's worker thread -- Playwright's sync API
    requires teardown to happen on the same thread the object was created on."""
    try:
        if session.context:
            session.context.close()
    except Exception:
        pass
    session.context = None
    session.page = None
    session.http_session = None
    session.http_csrf = None
    stop_bridge(session.bridge_proc)
    session.bridge_proc = None


async def close_browser(session):
    """Tear down just this session's browser context (keeping its slot's
    Chromium process running) so a retry (e.g. 'phone already taken') is fast."""
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(_pw_executors[session.slot], _blocking_close_context, session)


async def end_session(session):
    """Alias kept for call-site clarity; the slot's browser process itself
    outlives any single session and is only shut down when the bot exits."""
    await close_browser(session)


def _blocking_fill_and_register(session, phone):
    """Runs on session.slot's worker thread. Opens a fresh browser context on
    that slot's browser, fills the form, submits, and waits for the OTP screen."""
    browser = _blocking_ensure_browser(session.slot)
    proxy_conf = parse_proxy(session.proxy) if session.proxy else None
    try:
        proxy_conf, session.bridge_proc = maybe_bridge_proxy(proxy_conf)
    except RuntimeError as e:
        return {"ok": False, "message": f"Proxy bridge failed to start: {e}"}
    context = browser.new_context(proxy=proxy_conf) if proxy_conf else browser.new_context()
    page = context.new_page()
    session.context, session.page = context, page

    acct = session.acct
    try:
        page.goto(session.site_url or BOT_SITE_URL, wait_until="domcontentloaded", timeout=60000)
    except PWError as e:
        return {"ok": False, "message": f"Couldn't load the site (check the proxy?): {str(e)[:200]}"}
    page.wait_for_timeout(4000)

    if not open_signup_modal(page):
        SHOTS_DIR.mkdir(exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        no_modal_shot = SHOTS_DIR / f"{acct['username']}-{stamp}-no-modal.png"
        page.screenshot(path=str(no_modal_shot))
        return {"ok": False, "message": "Could not open the signup modal (JOIN button).",
                "shot": str(no_modal_shot)}

    fill_register_form(page, acct)

    SHOTS_DIR.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    page.screenshot(path=str(SHOTS_DIR / f"{acct['username']}-{stamp}-filled.png"))

    # submit_register clicks REGISTER and, if the register POST is AWS WAF
    # CAPTCHA-blocked and CAPSOLVER_API_KEY is set, solves it (via CapSolver)
    # and resubmits in a FRESH context -- all shared with the CLI. Without a
    # key it's a plain submit and `captured` still lets us report the block.
    # It may return a different page/context than we passed in (verified
    # live: the WAF keeps flagging the original context even with a valid
    # token, so the retry opens a new one and closes the old) -- sync
    # session.context/session.page so OTP verify and cleanup use the live one.
    outcome, msgs, captured, page = submit_register(page, acct, session.site_url,
                                                     proxy=session.proxy)
    session.context, session.page = page.context, page

    result_shot = SHOTS_DIR / f"{acct['username']}-{stamp}-result.png"
    page.screenshot(path=str(result_shot))

    if outcome == "phone_taken":
        return {"ok": False, "phone_taken": True, "message": check_phone_taken(page),
                "shot": str(result_shot)}

    if outcome in ("error", "timeout"):
        message = "Register rejected: " + ("; ".join(msgs) or "unknown error")
        if not msgs and is_waf_captcha(captured):
            action = captured.get("action") or "captcha"
            resp = captured.get("response")
            status = resp.status if resp else "?"
            hint = ("" if capsolver_key()
                    else " -- set CAPSOLVER_API_KEY in .env to auto-solve it")
            message += (f" | BLOCKED by AWS WAF (x-amzn-waf-action: {action}, "
                        f"HTTP {status}){hint}")
        elif not msgs and outcome == "timeout":
            message += " (no register API call was made -- REGISTER click had no effect)"
        return {"ok": False, "message": message,
                "shot": str(result_shot)}

    digits = page.locator(profile_for(page.url).sel["otp_digits"]).count()
    if digits == 0:
        return {"ok": False, "message": "OTP screen detected but no digit inputs found.",
                "shot": str(result_shot)}
    return {"ok": True, "digits": digits, "shot": str(result_shot)}


def _blocking_verify_otp(session, otp):
    """Runs on the SAME worker thread as _blocking_fill_and_register."""
    page = session.page
    acct = session.acct
    prof = profile_for(page.url)
    boxes = page.locator(prof.sel["otp_digits"])
    for i, ch in enumerate(otp):
        box = boxes.nth(i)
        box.click()
        box.press_sequentially(ch, delay=40)
    page.wait_for_timeout(500)

    stamp = time.strftime("%Y%m%d-%H%M%S")
    otp_filled = SHOTS_DIR / f"{acct['username']}-{stamp}-otp-filled.png"
    page.screenshot(path=str(otp_filled))

    if not click_first_visible(page, prof.sel["otp_verify"], timeout=6000):
        return {"ok": False, "message": "Could not find a visible Verify button.", "shot": str(otp_filled)}
    outcome = wait_for_otp_outcome(page)

    otp_result = SHOTS_DIR / f"{acct['username']}-{stamp}-otp-result.png"
    page.screenshot(path=str(otp_result))

    if outcome == "error":
        err = ""
        try:
            e = page.locator(prof.sel["otp_error"]).first
            if e.count() and e.is_visible():
                err = (e.inner_text() or "").strip()
        except Exception:
            pass
        return {"ok": False, "message": f"OTP rejected: {err}" if err else "OTP rejected.",
                "shot": str(otp_result)}
    if outcome == "timeout":
        return {"ok": False, "message": "OTP screen still showing — likely wrong/expired code.",
                "shot": str(otp_result)}
    return {"ok": True, "message": "OTP verified — account registered.", "shot": str(otp_result)}


# --- HTTP-fast mode (no browser) -- see /fast and main.py's http_signup_once ---
#
# Unlike _blocking_fill_and_register()/_blocking_verify_otp() above, these two
# don't touch Playwright at all, so they have no thread-affinity requirement --
# handle_message() dispatches them to asyncio's default executor (None), not a
# _pw_executors[slot], so HTTP-fast signups don't queue behind (or block)
# browser-based ones sharing the same slot.

def _blocking_http_register(session, phone):
    """HTTP-fast counterpart to _blocking_fill_and_register(): GET the site for
    a CSRF token, then POST /register with otp="" to trigger the SMS. Returns
    the same result shape handle_message() already expects
    (ok/phone_taken/message/shot/digits), so its await_phone branch can treat
    this and the browser path identically. Stashes the requests.Session + CSRF
    token on `session` for the OTP-verify call that follows."""
    url = session.site_url or BOT_SITE_URL
    prof = profile_for(url)
    acct = session.acct

    try:
        http_sess = http_session_for(session.proxy)
    except ValueError as e:
        return {"ok": False, "message": f"Bad proxy: {e}"}

    try:
        csrf = http_fetch_csrf(http_sess, url)
    except (requests.RequestException, RuntimeError) as e:
        return {"ok": False, "message": f"Couldn't load the site (check the proxy?): {str(e)[:200]}"}

    try:
        resp_json = http_register_call(http_sess, csrf, acct, url, otp="")
    except requests.RequestException as e:
        return {"ok": False, "message": f"Register request failed: {str(e)[:200]}"}

    if http_is_error(resp_json):
        if http_is_phone_taken(resp_json):
            return {"ok": False, "phone_taken": True, "message": resp_json.get("message")}
        return {"ok": False, "message": "Register rejected: " + (resp_json.get("message") or str(resp_json))}

    session.http_session = http_sess
    session.http_csrf = csrf
    return {"ok": True, "digits": prof.http_otp_digits, "shot": None}


def _blocking_http_verify_otp(session, otp):
    """HTTP-fast counterpart to _blocking_verify_otp(): re-POST /register on
    the SAME requests.Session, now with the real code."""
    url = session.site_url or BOT_SITE_URL
    try:
        verify_json = http_register_call(session.http_session, session.http_csrf,
                                         session.acct, url, otp=otp)
    except requests.RequestException as e:
        return {"ok": False, "message": f"OTP verify request failed: {str(e)[:200]}", "shot": None}

    if http_is_error(verify_json):
        return {"ok": False, "message": f"OTP rejected: {verify_json.get('message') or verify_json}",
                "shot": None}
    return {"ok": True, "message": verify_json.get("message") or "OTP verified — account registered.",
            "shot": None}


async def begin_signup(update, chat_id, sub_id):
    """Generate a new account and start a fresh session in lane sub_id.
    Shared by /newacc (each initial lane) and the continuous-mode
    auto-restart in handle_message() after that lane's signup finishes."""
    session = Session()
    session.acct = gen_account()
    # A master-set fixed password (/setpassword) overrides the random one
    # gen_account() just generated; /setpassword --random clears this so the
    # random default applies again.
    if global_settings.get("password"):
        session.acct["password"] = global_settings["password"]
    session.stage = "await_phone"
    # Proxy/URL are set globally by the master admin (/setproxy, /seturl),
    # applying to every admin's signups -- not a per-chat setting.
    session.proxy = global_settings.get("proxy")
    session.site_url = global_settings.get("url")
    session.sub_id = sub_id
    # Pins this session to one (executor, Chromium) slot for its whole life --
    # round-robin across BOT_CONCURRENCY slots so concurrent lanes (whether
    # from different chats or several lanes in one /newacc <n>) run on
    # different browsers/threads instead of queuing behind each other. Unused
    # (but still assigned, for simplicity) when use_fast is True below -- an
    # HTTP-fast session never touches _pw_executors[session.slot].
    session.slot = _next_slot()

    # /fast (global_settings["fast"]) requests HTTP-fast mode, but it only
    # actually applies if THIS signup's site supports it (supports_http_fast,
    # see sites/base.py) -- otherwise fall back to the browser, same as the
    # CLI's --fast. Decided once here (not per-message) since the site URL is
    # fixed for a session's whole life.
    fast_wanted = bool(global_settings.get("fast"))
    prof = profile_for(session.site_url or BOT_SITE_URL)
    session.use_fast = fast_wanted and prof.supports_http_fast
    fallback_note = ""
    if fast_wanted and not prof.supports_http_fast:
        fallback_note = f" (⚡ fast mode is ON, but {prof.key} needs a real browser — using it for this one)"

    sessions.setdefault(chat_id, {})[sub_id] = session

    tag = "⚡ " if session.use_fast else ""
    await update.message.reply_text(
        f"{tag}📱 [#{sub_id}] Send the phone number to use for this signup.{fallback_note}")


@require_role(is_admin)
async def newacc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if sessions.get(chat_id):
        active = ", ".join(str(i) for i in sorted(sessions[chat_id]))
        await update.message.reply_text(
            f"You already have signup(s) in progress (lane(s) {active}). Reply with "
            "\"<lane> <phone/OTP>\" for the one it's waiting on, or send /cancel."
        )
        return

    count = 1
    if context.args:
        arg = context.args[0]
        if not arg.isdigit() or not (1 <= int(arg) <= MAX_PARALLEL_NEWACC):
            await update.message.reply_text(
                f"Usage: /newacc [count]  (count 1-{MAX_PARALLEL_NEWACC}, default 1)"
            )
            return
        count = int(arg)

    # Each lane now runs continuously: after that lane's signup finishes, a
    # new one starts automatically in the same lane until /done (or /cancel)
    # removes it.
    for sub_id in range(1, count + 1):
        looping_chats.add((chat_id, sub_id))
        await begin_signup(update, chat_id, sub_id)


@require_role(is_admin)
async def done_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if context.args and context.args[0].isdigit():
        lanes = [int(context.args[0])]
    else:
        lanes = sorted({sid for (cid, sid) in looping_chats if cid == chat_id}
                        | set(sessions.get(chat_id, {})))
    if not lanes:
        await update.message.reply_text("Not in continuous mode.")
        return

    stopped, in_progress = [], []
    for sub_id in lanes:
        was_looping = (chat_id, sub_id) in looping_chats
        looping_chats.discard((chat_id, sub_id))
        if not was_looping:
            continue
        (in_progress if sub_id in sessions.get(chat_id, {}) else stopped).append(sub_id)

    if not stopped and not in_progress:
        await update.message.reply_text("Not in continuous mode.")
        return
    parts = []
    if in_progress:
        parts.append(f"Will stop after the current signup finishes (lane(s) {', '.join(map(str, in_progress))}).")
    if stopped:
        parts.append(f"Continuous mode stopped (lane(s) {', '.join(map(str, stopped))}).")
    await update.message.reply_text(" ".join(parts))


@require_role(is_admin)
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if context.args and context.args[0].isdigit():
        lanes = [int(context.args[0])]
    else:
        lanes = sorted({sid for (cid, sid) in looping_chats if cid == chat_id}
                        | set(sessions.get(chat_id, {})))

    cancelled = []
    for sub_id in lanes:
        looping_chats.discard((chat_id, sub_id))
        session = sessions.get(chat_id, {}).pop(sub_id, None)
        if session:
            await end_session(session)
            cancelled.append(sub_id)
    if chat_id in sessions and not sessions[chat_id]:
        del sessions[chat_id]

    if cancelled:
        await update.message.reply_text(
            f"🛑 Cancelled signup(s) in lane(s) {', '.join(map(str, cancelled))}."
        )
    else:
        await update.message.reply_text("No signup in progress.")


@require_role(is_master)
async def setpassword(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "Usage: /setpassword <password>  (fixed password for every future signup)\n"
            "/setpassword --random  (back to a random password per signup, the default)"
        )
        return
    if context.args[0] == "--random":
        if global_settings.pop("password", None) is not None:
            save_settings()
        await update.message.reply_text(
            "🔑 Password mode: RANDOM — each new signup gets its own random password again."
        )
        return
    password = context.args[0]
    global_settings["password"] = password
    save_settings()
    await update.message.reply_text(
        f"🔑 Password mode: FIXED — every future signup will use: {password}\n"
        "Use /setpassword --random to go back to random passwords."
    )


@require_role(is_master)
async def show_password(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pw = global_settings.get("password")
    if pw:
        await update.message.reply_text(f"🔑 Password mode: FIXED — {pw}")
    else:
        await update.message.reply_text("🔑 Password mode: RANDOM (default, per-signup)")


@require_role(is_master)
async def fast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/fast on|off -- toggle HTTP-fast signup mode globally (applies to every
    admin's /newacc). When ON, each signup skips the browser entirely for
    sites whose profile sets supports_http_fast=True (currently cricmatch247
    only, see sites/cricmatch.py and CLAUDE.md's "HTTP-fast signup" section) --
    a real browser is still used automatically for any site that doesn't
    support it (spin24star: its register POST is WAF-gated behind a JS
    challenge, see main.py). No args shows the current state."""
    if not context.args:
        state = "ON" if global_settings.get("fast") else "OFF"
        await update.message.reply_text(
            f"⚡ Fast mode: {state}\n"
            "Usage: /fast on | /fast off\n\n"
            "When ON, /newacc skips the browser and hits the register API "
            "directly (cricmatch247 only, ~10-20x faster) -- more fragile to "
            "backend changes than driving the real form. Sites that need a "
            "real browser (e.g. spin24star's WAF challenge) fall back "
            "automatically either way."
        )
        return
    arg = context.args[0].lower()
    if arg not in ("on", "off"):
        await update.message.reply_text("Usage: /fast on | /fast off")
        return
    global_settings["fast"] = (arg == "on")
    save_settings()
    await update.message.reply_text(f"⚡ Fast mode: {'ON' if arg == 'on' else 'OFF'}")


@require_role(is_master)
async def setproxy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "Usage: /setproxy host:port:username:password\n"
            "Also accepts host:port, or a scheme://user:pass@host:port URL."
        )
        return
    raw = context.args[0]
    try:
        parse_proxy(raw)
    except ValueError as e:
        await update.message.reply_text(f"Invalid proxy: {e}")
        return
    global_settings["proxy"] = raw
    save_settings()
    await update.message.reply_text(
        f"🌐 Global proxy set: {mask_proxy_display(raw)}\n"
        "💡 Tip: /testproxy to confirm it works before /newacc."
    )


@require_role(is_master)
async def show_proxy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = global_settings.get("proxy")
    if raw:
        await update.message.reply_text(f"🌐 Current global proxy: {mask_proxy_display(raw)}")
    else:
        await update.message.reply_text("🌐 No proxy set — signups use a direct connection.")


@require_role(is_master)
async def clearproxy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if global_settings.pop("proxy", None) is not None:
        save_settings()
        await update.message.reply_text("🌐 Global proxy cleared — signups will use a direct connection.")
    else:
        await update.message.reply_text("No proxy was set.")


@require_role(is_master)
async def seturl(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /seturl https://example.com")
        return
    url = context.args[0]
    if not (url.startswith("http://") or url.startswith("https://")):
        await update.message.reply_text("URL must start with http:// or https://")
        return
    global_settings["url"] = url
    save_settings()
    await update.message.reply_text(f"🔗 Global site URL set: {url}")


@require_role(is_master)
async def show_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = global_settings.get("url")
    await update.message.reply_text(f"🔗 Current global site URL: {url or BOT_SITE_URL}"
                                    + ("" if url else " (default)"))


@require_role(is_master)
async def clearurl(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if global_settings.pop("url", None) is not None:
        save_settings()
        await update.message.reply_text(f"🔗 Site URL reset to default: {BOT_SITE_URL}")
    else:
        await update.message.reply_text("No custom site URL was set.")


@require_role(is_master)
async def btag_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set (or show) just the btag affiliate/referral code, keeping whatever
    scheme/host/path the global site URL (or this bot's own default) already
    has -- so the master doesn't have to retype the whole URL to swap tags."""
    current_url = global_settings.get("url") or BOT_SITE_URL
    if not context.args:
        code = extract_referral_code(current_url)
        await update.message.reply_text(
            f"🏷 Current btag: {code}" if code else "🏷 No btag set on the current site URL."
        )
        return
    code = context.args[0]
    parts = urlsplit(current_url)
    new_url = urlunsplit((parts.scheme, parts.netloc, parts.path, f"btag={code}", parts.fragment))
    global_settings["url"] = new_url
    save_settings()
    await update.message.reply_text(f"🏷 Global site URL set: {new_url}")


def _blocking_test_proxy_once(proxy_conf, timeout_ms=30000):
    # Always slot 0 -- this is an on-demand diagnostic, not a throughput path,
    # so it's fine to briefly share slot 0's browser with a live signup.
    browser = _blocking_ensure_browser(0)
    bridge_proc = None
    try:
        proxy_conf, bridge_proc = maybe_bridge_proxy(proxy_conf)
    except RuntimeError as e:
        return {"ok": False, "error": f"Proxy bridge failed to start: {e}"}
    context = browser.new_context(proxy=proxy_conf)
    try:
        page = context.new_page()
        page.goto("https://api.ipify.org/?format=json", timeout=timeout_ms)
        text = page.inner_text("body").strip()
        return {"ok": True, "ip_info": text}
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}
    finally:
        context.close()
        stop_bridge(bridge_proc)


def _blocking_test_proxy(proxy_conf):
    """Runs on _pw_executors[0]. Opens a throwaway context with the given proxy
    and hits an IP-echo service to confirm it actually routes traffic. If an
    http(s):// proxy times out, automatically retries once as socks5:// --
    many proxy resellers (ProxyCheap included) issue SOCKS5-only endpoints
    that look identical to an HTTP proxy string."""
    result = _blocking_test_proxy_once(proxy_conf)
    if result["ok"]:
        return result

    server = proxy_conf.get("server", "")
    if server.startswith("http://") or server.startswith("https://"):
        alt = dict(proxy_conf)
        alt["server"] = "socks5://" + server.split("://", 1)[1]
        alt_result = _blocking_test_proxy_once(alt)
        if alt_result["ok"]:
            alt_result["note"] = ("Worked as socks5://, not http:// -- use a socks5:// "
                                  "prefix for this proxy going forward.")
            return alt_result
        result["also_tried_socks5"] = True
    return result


@require_role(is_master)
async def testproxy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = context.args[0] if context.args else global_settings.get("proxy")
    if not raw:
        await update.message.reply_text(
            "No proxy to test. Use /testproxy host:port:username:password, or /setproxy first."
        )
        return
    try:
        proxy_conf = parse_proxy(raw)
    except ValueError as e:
        await update.message.reply_text(f"Invalid proxy: {e}")
        return

    await update.message.reply_text(f"Testing {mask_proxy_display(raw)} (may take up to 30-60s)...")
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(_pw_executors[0], _blocking_test_proxy, proxy_conf)
    if result["ok"]:
        reply = f"Proxy works. Exit IP: {result['ip_info']}"
        if result.get("note"):
            reply += f"\n\n{result['note']}"
        await update.message.reply_text(reply)
    else:
        reply = f"Proxy failed: {result['error']}"
        if result.get("also_tried_socks5"):
            reply += "\n(also tried socks5:// -- that failed too)"
        reply += (
            "\n\nA timeout (rather than an immediate auth error) usually means "
            "either the wrong protocol or the proxy needs your current IP "
            "whitelisted. Check your ProxyCheap dashboard for: (1) whether "
            "auth is username/password or IP-whitelist mode, (2) whether this "
            "is an HTTP or SOCKS5 endpoint, (3) that the proxy isn't expired."
        )
        await update.message.reply_text(reply)


def _blocking_test_baccarat(username, password, amount):
    """Runs on _pw_executors[0]. Opens a throwaway context (mirrors
    _blocking_test_proxy_once's "share slot 0, always clean up" pattern) and
    calls main.test_baccarat() to log in and place a real bet. Uses the global
    proxy (like signups/hedge) so a datacenter box whose IP the site WAF-blocks
    can still reach the login/casino."""
    browser = _blocking_ensure_browser(0)
    raw = global_settings.get("proxy")
    proxy_conf = parse_proxy(raw) if raw else None
    bridge_proc = None
    try:
        proxy_conf, bridge_proc = maybe_bridge_proxy(proxy_conf)
    except RuntimeError as e:
        return {"ok": False, "messages": [f"Proxy bridge failed to start: {e}"], "shot": None}
    context = browser.new_context(proxy=proxy_conf) if proxy_conf else browser.new_context()
    try:
        page = context.new_page()
        return test_baccarat(page, username, password, amount, site_url=BOT_SITE_URL)
    except PWError as e:
        return {"ok": False, "messages": [f"Playwright error: {str(e)[:300]}"], "shot": None}
    finally:
        context.close()
        stop_bridge(bridge_proc)


@require_role(is_master)
async def testbaccarat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Login + place a REAL bet on both Player and Banker in a live Baccarat
    table, to smoke-test that the third-party casino game integration works.
    Master-only: this spends real money and takes another account's
    credentials as a chat argument, so it gets the same restricted scope as
    /testproxy and /setpassword rather than being admin-usable. Verified live
    against cricmatch247 only -- see main.py's casino-testing section."""
    args = context.args
    if len(args) < 2:
        await update.message.reply_text(
            "Usage: /testbaccarat <username> <password> [amount]\n\n"
            "Logs into an EXISTING account and places a REAL bet on both "
            "Player and Banker in a live Baccarat table, to confirm the "
            "casino game integration actually works. This spends real "
            "money -- amount defaults to 100 (the table's minimum). Chip "
            "denomination isn't selectable, so the amount placed may not "
            "exactly match what you ask for; the reply reports what was "
            "actually placed, read back from the game itself."
        )
        return

    username, password = args[0], args[1]
    amount = 100
    if len(args) >= 3:
        try:
            amount = int(args[2])
        except ValueError:
            await update.message.reply_text("Amount must be a whole number.")
            return
        if amount <= 0:
            await update.message.reply_text("Amount must be positive.")
            return

    await update.message.reply_text(
        f"Testing Baccarat as {username}: {amount} on Player + {amount} on Banker "
        "(real money, may take up to a minute)..."
    )
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(_pw_executors[0], _blocking_test_baccarat,
                                        username, password, amount)

    status = "OK" if result["ok"] else "FAILED"
    caption = f"Baccarat test [{status}] for {username}\n" + "\n".join(result["messages"])
    await send_result_photo(update, result.get("shot"), caption[:1024])


# --- Paired-account hedge betting (create pairs, run opposite-side bets) ---

def _blocking_run_pair(loop, bot, chat_id, pid, banker_creds, player_creds, amount, rounds, stop_event):
    """Runs on _run_executor -- one worker thread per concurrent /run, fully
    independent of _pw_executors/slot 0 and of every other concurrent run.
    Drives main.run_paired_hedge with browser=None, so it launches its OWN
    temporary Banker browser on this thread in addition to the temporary
    Player browser + worker thread it already spins up internally -- two
    fully separate runs therefore share no browser, thread, or Playwright
    object at all. Streams per-round RESULTS back to the chat with
    run_coroutine_threadsafe (the asyncio loop lives on the bot's own thread,
    not this worker thread), prefixed with the pair id so concurrent runs'
    messages don't get confused for each other in the same chat. Setup-phase
    chatter (logging in, opening the casino, joining the table, retries) goes
    to the console log ONLY via setup_progress -- the chat just gets the
    "Run started" card, each hedged round, and the final summary."""

    def progress(text):
        logger.info(f"[Pair #{pid}] {text}")
        try:
            asyncio.run_coroutine_threadsafe(
                bot.send_message(chat_id, f"[Pair #{pid}] {text}"), loop)
        except Exception:
            pass

    def setup_progress(text):
        logger.info(f"[Pair #{pid}] {text}")

    try:
        return run_paired_hedge(
            banker_creds, player_creds, amount, rounds,
            site_url=BOT_SITE_URL, progress=progress, setup_progress=setup_progress,
            should_stop=stop_event.is_set,
            proxy=global_settings.get("proxy"), game=RUN_GAME)
    except PWError as e:
        return {"ok": False, "rounds_done": 0, "requested_rounds": rounds,
                "stop_reason": "playwright_error",
                "messages": [f"Playwright error: {str(e)[:300]}"],
                "shots": [], "game": RUN_GAME.key,
                "final_balance": {"banker": None, "player": None}}


@require_role(is_master)
async def cpair(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/pair <user1> <pass1> <user2> <pass2> -- store an account pair for hedge
    betting. Account 1 always bets BANKER, account 2 always bets PLAYER.
    Master-only; the reply never echoes the passwords back."""
    args = context.args
    if len(args) != 4:
        await update.message.reply_text(
            "Usage: /pair <user1> <pass1> <user2> <pass2>\n"
            "Creates a pair: account 1 bets BANKER, account 2 bets PLAYER.")
        return
    u1, p1, u2, p2 = args
    pid = str(pairs["next_id"])
    pairs["next_id"] += 1
    pairs["pairs"][pid] = {
        "banker": {"username": u1, "password": p1},
        "player": {"username": u2, "password": p2},
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    save_pairs()
    await update.message.reply_text(
        f"✅ <b>Pair #{pid} created</b>\n"
        f"━━━━━━━━━━━━━━\n"
        f"{RUN_GAME.side_a_icon} {RUN_GAME.side_a_label}  <b>{html.escape(u1)}</b>\n"
        f"{RUN_GAME.side_b_icon} {RUN_GAME.side_b_label}  <b>{html.escape(u2)}</b>\n"
        f"━━━━━━━━━━━━━━\n"
        f"▶️ Run it: /run {pid} &lt;amount&gt; &lt;rounds&gt;",
        parse_mode="HTML")


@require_role(is_master)
async def pairs_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/pairs -- list stored pairs (passwords omitted)."""
    if not pairs["pairs"]:
        await update.message.reply_text("No pairs yet. Create one with /pair.")
        return
    lines = ["👥 <b>Stored pairs</b>", "━━━━━━━━━━━━━━"]
    for pid, rec in sorted(pairs["pairs"].items(), key=lambda kv: int(kv[0])):
        created = (rec.get("created_at") or "").replace("T", " ")[:16]
        running = "  🏃 running" if pid in _active_runs else ""
        lines.append(
            f"<b>#{pid}</b>   {RUN_GAME.side_a_icon} {html.escape(rec['banker']['username'])}"
            f"   {RUN_GAME.side_b_icon} {html.escape(rec['player']['username'])}{running}\n"
            f"   🕒 {created}")
    lines.append("\n▶️ Run one with /run &lt;id&gt; &lt;amount&gt; &lt;rounds&gt;")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


@require_role(is_master)
async def delpair(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/delpair <id> -- remove a stored pair."""
    if not context.args:
        await update.message.reply_text("Usage: /delpair <id>")
        return
    pid = context.args[0]
    if pid not in pairs["pairs"]:
        await update.message.reply_text(f"No pair #{pid}. See /pairs.")
        return
    if pid in _active_runs:
        await update.message.reply_text(f"Pair #{pid} is running. /stoprun {pid} first.")
        return
    pairs["pairs"].pop(pid)
    save_pairs()
    await update.message.reply_text(f"🗑 Pair #{pid} deleted.")


@require_role(is_master)
async def run_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/run <pair_id> <amount> <rounds> -- run the paired hedge: acc1 bets
    Banker and acc2 bets Player, <amount> each, on the SAME hand, repeating
    until <rounds> or one account runs low on balance. Real money.

    Several /run calls for DIFFERENT pairs can be active at once (up to
    MAX_CONCURRENT_RUNS) -- each drives two fully independent temporary
    browsers, see run_paired_hedge in main.py. A pair already running (or one
    that shares an account with a currently-running pair) is refused a second
    concurrent /run."""
    args = context.args
    if len(args) != 3:
        await update.message.reply_text(
            "Usage: /run <pair_id> <amount> <rounds>\n"
            "Logs into the pair, joins the SAME baccarat table, and each round "
            "bets <amount> on Banker (acc1) and <amount> on Player (acc2) on the "
            "same hand, until <rounds> is reached or one account runs low. Real "
            "money. v1 uses the table's default chip (~Rs.100); if <amount> "
            "doesn't match, it stops after one hedged round and reports the real "
            "size. Multiple pairs can run at once (up to "
            f"{MAX_CONCURRENT_RUNS}); see /runs for what's active.")
        return
    pid, amount_s, rounds_s = args
    if pid not in pairs["pairs"]:
        await update.message.reply_text(f"No pair #{pid}. See /pairs.")
        return
    try:
        amount, rounds = int(amount_s), int(rounds_s)
    except ValueError:
        await update.message.reply_text("amount and rounds must be whole numbers.")
        return
    if amount <= 0 or rounds <= 0:
        await update.message.reply_text("amount and rounds must be positive.")
        return
    if pid in _active_runs:
        await update.message.reply_text(f"Pair #{pid} is already running. /stoprun {pid} first.")
        return
    if len(_active_runs) >= MAX_CONCURRENT_RUNS:
        await update.message.reply_text(
            f"{MAX_CONCURRENT_RUNS} runs are already active (the max). "
            "/stoprun one first, or raise MAX_CONCURRENT_RUNS.")
        return
    rec = pairs["pairs"][pid]
    banker_user, player_user = rec["banker"]["username"], rec["player"]["username"]
    busy = {u for r in _active_runs.values() for u in (r["banker"], r["player"])}
    if banker_user in busy or player_user in busy:
        await update.message.reply_text(
            f"One of pair #{pid}'s accounts is already in use by another active run.")
        return

    stop_event = threading.Event()
    _active_runs[pid] = {"stop_event": stop_event, "banker": banker_user, "player": player_user}
    raw_proxy = global_settings.get("proxy")
    proxy_line = (f"🌐 Proxy   {html.escape(mask_proxy_display(raw_proxy))}"
                  if raw_proxy else "🌐 Proxy   none (direct connection)")
    await update.message.reply_text(
        f"🎰 <b>Run started · Pair #{pid}</b>\n"
        f"━━━━━━━━━━━━━━\n"
        f"{RUN_GAME.side_a_icon} {RUN_GAME.side_a_label}  <b>{html.escape(rec['banker']['username'])}</b>\n"
        f"{RUN_GAME.side_b_icon} {RUN_GAME.side_b_label}  <b>{html.escape(rec['player']['username'])}</b>\n"
        f"💵 Stake   <b>₹{amount:,}</b> / side\n"
        f"🔁 Rounds  up to <b>{rounds}</b>\n"
        f"{proxy_line}\n"
        f"━━━━━━━━━━━━━━\n"
        f"⚠️ Real money · send /stoprun {pid} to halt",
        parse_mode="HTML")
    loop = asyncio.get_running_loop()
    try:
        summary = await loop.run_in_executor(
            _run_executor, _blocking_run_pair, loop, context.bot,
            update.effective_chat.id, pid, rec["banker"], rec["player"], amount, rounds,
            stop_event)
    finally:
        _active_runs.pop(pid, None)

    # Persist the run so it can be reviewed later via /runs, then save.
    run_id = pair_runs["next_id"]
    pair_runs["next_id"] += 1
    pair_runs["runs"].append({
        "run_id": run_id,
        "pair_id": pid,
        "banker_username": rec["banker"]["username"],
        "player_username": rec["player"]["username"],
        "amount": amount,
        "requested_rounds": summary["requested_rounds"],
        "rounds_done": summary["rounds_done"],
        "stop_reason": summary["stop_reason"],
        "mode": summary.get("game", RUN_GAME.key),
        "started_at": summary.get("started_at"),
        "ended_at": summary.get("ended_at"),
        "start_balance": summary.get("start_balance", {}),
        "final_balance": summary.get("final_balance", {}),
        "rounds": summary.get("rounds", []),
        "unhedged_rounds": summary.get("unhedged_rounds", []),
        "messages": summary.get("messages", []),
        "cashout_trace": summary.get("cashout_trace", []),
        "cashout_live_dump": summary.get("cashout_live_dump", []),
        "shots": summary.get("shots", []),
    })
    save_pair_runs()

    sb, fb = summary.get("start_balance", {}), summary.get("final_balance", {})
    done, req = summary["rounds_done"], summary["requested_rounds"]
    icon = "✅" if summary.get("ok") else "⚠️"
    b_user = html.escape(rec["banker"]["username"])
    p_user = html.escape(rec["player"]["username"])
    lines = [
        f"{icon} <b>Run #{run_id} finished · Pair #{pid}</b>",
        f"━━━━━━━━━━━━━━",
        f"🎯 Rounds hedged  <b>{done}/{req}</b>",
        f"🛑 {_reason_label(summary['stop_reason'])}",
        f"━━━━━━━━━━━━━━",
        f"{RUN_GAME.side_a_icon} {b_user}   {_bal(fb.get('banker'))}  ({_net_tag(sb.get('banker'), fb.get('banker'))})",
        f"{RUN_GAME.side_b_icon} {p_user}   {_bal(fb.get('player'))}  ({_net_tag(sb.get('player'), fb.get('player'))})",
    ]
    notes = [m for m in summary.get("messages", []) if m]
    if notes:
        lines.append("━━━━━━━━━━━━━━")
        lines += [f"ℹ️ {html.escape(m)}" for m in notes]
    lines.append(f"\n📄 History: /runs {pid}  ·  /runlog {run_id}")
    await update.message.reply_text("\n".join(lines)[:4000], parse_mode="HTML")
    for shot in summary.get("shots", []):
        try:
            with open(shot, "rb") as f:
                await update.message.reply_photo(photo=f)
        except Exception:
            pass


@require_role(is_master)
async def stoprun(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/stoprun [pair_id] -- ask one active run (or, with no argument, EVERY
    active run) to stop after its current round. Several runs can be active
    at once now, so a bare /stoprun stops all of them rather than one implicit
    run."""
    if not _active_runs:
        await update.message.reply_text("No run is in progress.")
        return
    args = context.args
    if args:
        pid = args[0]
        if pid not in _active_runs:
            await update.message.reply_text(
                f"Pair #{pid} isn't running. Active: {', '.join(sorted(_active_runs, key=int))}")
            return
        _active_runs[pid]["stop_event"].set()
        await update.message.reply_text(f"🛑 Stopping pair #{pid} after its current round…")
        return
    for rec in _active_runs.values():
        rec["stop_event"].set()
    await update.message.reply_text(
        f"🛑 Stopping {len(_active_runs)} active run(s) after their current round…")


def _net(start, final):
    """Signed net change (final - start) as a display string, or '?' if either
    balance couldn't be read."""
    if start is None or final is None:
        return "?"
    d = final - start
    return f"+{d}" if d >= 0 else str(d)


# Human-readable labels for run stop reasons (plain words, per user preference).
_REASON_LABEL = {
    "completed": "Completed all rounds",
    "stopped_by_user": "Stopped by you",
    "no_open_window": "Missed the betting window too many times in a row",
    "setup_failed": "Could not open the tables",
    "different_tables": "Accounts landed on different tables",
    "repeated_unhedged_exposure": "Too many one-sided landings in a row",
    "max_attempts_exceeded": "Gave up retrying before reaching the requested rounds",
    "amount_mismatch": "Chip size didn't match",
    "banker_out_of_balance": f"{RUN_GAME.side_a_label} side ran low on balance",
    "player_out_of_balance": f"{RUN_GAME.side_b_label} side ran low on balance",
    "playwright_error": "Browser error",
    # Stock Market Live only -- see the cash-out block in run_paired_hedge.
    "no_cashout_window": "No cash-out window appeared",
    "cashout_partial": "Safety stop (one side still riding)",
    "cashout_failed": "Neither side cashed out (still hedged)",
    "chip_select_failed": "Could not select the chip size",
    "cashout_divergence": "Cash-outs didn't land together",
}


def _reason_label(reason):
    return _REASON_LABEL.get(reason, (reason or "unknown").replace("_", " "))


def _bal(v):
    """Format a balance value for chat, or an em-dash if unknown."""
    return f"₹{v:,}" if isinstance(v, int) else "—"


def _net_tag(start, final):
    """Net change with a sign and colour word, e.g. '+₹100'. '—' if unknown."""
    if not isinstance(start, int) or not isinstance(final, int):
        return "—"
    d = final - start
    return f"+₹{d:,}" if d >= 0 else f"−₹{abs(d):,}"


@require_role(is_master)
async def runs_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/runs [pair_id] -- list past hedge runs, most recent first (all pairs,
    or just one pair_id). Use /runlog <run_id> for a run's per-round detail."""
    runs = pair_runs["runs"]
    filter_pid = None
    active = _active_runs
    if context.args:
        filter_pid = context.args[0]
        runs = [r for r in runs if r.get("pair_id") == filter_pid]
        active = {p: r for p, r in _active_runs.items() if p == filter_pid}
        header = f"Hedge runs · Pair #{filter_pid}"
        empty = f"No runs recorded for pair #{filter_pid} yet."
    else:
        header = "Recent hedge runs"
        empty = "No runs recorded yet. Start one with /run."
    if not runs and not active:
        await update.message.reply_text(empty)
        return
    lines = [f"📊 <b>{header}</b>", "━━━━━━━━━━━━━━"]
    if active:
        lines.append("🏃 <b>Active now</b>")
        for pid, rec in active.items():
            lines.append(f"   #{pid}  {RUN_GAME.side_a_icon} {html.escape(rec['banker'])}  {RUN_GAME.side_b_icon} {html.escape(rec['player'])}")
        lines.append("━━━━━━━━━━━━━━")
    for r in list(reversed(runs))[:15]:
        sb, fb = r.get("start_balance", {}), r.get("final_balance", {})
        icon = "✅" if r["stop_reason"] == "completed" else "⚠️"
        ended = (r.get("ended_at") or "").replace("T", " ")[:16]
        lines.append(
            f"{icon} <b>Run #{r['run_id']}</b> · pair {r['pair_id']} · "
            f"{r['rounds_done']}/{r['requested_rounds']} @ ₹{r['amount']:,}\n"
            f"   {RUN_GAME.side_a_icon} {html.escape(r['banker_username'])} {_net_tag(sb.get('banker'), fb.get('banker'))}"
            f"   {RUN_GAME.side_b_icon} {html.escape(r['player_username'])} {_net_tag(sb.get('player'), fb.get('player'))}\n"
            f"   {_reason_label(r['stop_reason'])} · {ended}")
    lines.append("\n📄 /runlog &lt;run_id&gt; for round-by-round detail")
    await update.message.reply_text("\n".join(lines)[:4000], parse_mode="HTML")


@require_role(is_master)
async def runlog_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/runlog <run_id> -- per-round balance progression of one past run."""
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /runlog <run_id>  (ids from /runs)")
        return
    rid = int(context.args[0])
    rec = next((r for r in pair_runs["runs"] if r["run_id"] == rid), None)
    if rec is None:
        await update.message.reply_text(f"No run #{rid}. See /runs.")
        return
    sb, fb = rec.get("start_balance", {}), rec.get("final_balance", {})
    b_user = html.escape(rec["banker_username"])
    p_user = html.escape(rec["player_username"])
    started = (rec.get("started_at") or "").replace("T", " ")
    ended = (rec.get("ended_at") or "").replace("T", " ")
    lines = [
        f"📄 <b>Run #{rid} · Pair #{rec['pair_id']}</b>",
        f"{RUN_GAME.side_a_icon} {b_user} ({RUN_GAME.side_a_label})  vs  "
        f"{RUN_GAME.side_b_icon} {p_user} ({RUN_GAME.side_b_label})",
        f"💵 ₹{rec['amount']:,}/side · 🎯 {rec['rounds_done']}/{rec['requested_rounds']} rounds",
        f"🛑 {_reason_label(rec['stop_reason'])}",
        f"🕒 {started} → {ended}",
        "━━━━━━━━━━━━━━",
        f"Start   {RUN_GAME.side_a_icon} {_bal(sb.get('banker'))}   {RUN_GAME.side_b_icon} {_bal(sb.get('player'))}",
    ]
    for rr in rec.get("rounds", []):
        lines.append(f"R{rr['round']}      {RUN_GAME.side_a_icon} {_bal(rr.get('banker'))}   {RUN_GAME.side_b_icon} {_bal(rr.get('player'))}")
    lines.append(
        f"Final   {RUN_GAME.side_a_icon} {_bal(fb.get('banker'))} ({_net_tag(sb.get('banker'), fb.get('banker'))})"
        f"   {RUN_GAME.side_b_icon} {_bal(fb.get('player'))} ({_net_tag(sb.get('player'), fb.get('player'))})")
    notes = [m for m in rec.get("messages", []) if m]
    if notes:
        lines.append("━━━━━━━━━━━━━━")
        lines += [f"ℹ️ {html.escape(m)}" for m in notes]
    await update.message.reply_text("\n".join(lines)[:4000], parse_mode="HTML")


@require_role(is_master)
async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """No args: overall status breakdown plus a per-btag count. One arg (a
    btag code): status breakdown for just that btag."""
    if context.args:
        btag = context.args[0]
        cur = conn.execute(
            "SELECT status, COUNT(*) FROM accounts WHERE referral_code = ? GROUP BY status",
            (btag,))
        rows = cur.fetchall()
        if not rows:
            await update.message.reply_text(f"No signups recorded for btag {btag}.")
            return
        total = sum(c for _, c in rows)
        lines = [f"📊 <b>btag {html.escape(btag)} — {total} signups</b>", "━━━━━━━━━━━━━━"]
        for status, count in rows:
            lines.append(f"   {html.escape(status)}:  <b>{count}</b>")
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")
        return

    cur = conn.execute("SELECT status, COUNT(*) FROM accounts GROUP BY status")
    rows = cur.fetchall()
    total = sum(c for _, c in rows)
    if not rows:
        await update.message.reply_text("No signups recorded yet.")
        return
    lines = [f"📊 <b>Signups — {total} total</b>", "━━━━━━━━━━━━━━", "<b>By status</b>"]
    for status, count in rows:
        lines.append(f"   {html.escape(status)}:  <b>{count}</b>")

    btag_rows = conn.execute(
        "SELECT COALESCE(referral_code, '(none)'), COUNT(*) FROM accounts "
        "GROUP BY referral_code ORDER BY COUNT(*) DESC"
    ).fetchall()
    lines.append("")
    lines.append("<b>By btag</b>")
    for code, count in btag_rows:
        lines.append(f"   {html.escape(str(code))}:  <b>{count}</b>")

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


@require_role(is_master)
async def list_accounts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    limit = 10
    if context.args:
        try:
            limit = max(1, min(int(context.args[0]), 50))
        except ValueError:
            pass
    rows = db.list_accounts(conn, limit=limit)
    if not rows:
        await update.message.reply_text("No accounts stored yet.")
        return

    lines = []
    for row in rows:
        r = dict(zip(db.COLUMNS, row))
        line = (
            f"🧾 #{r['id']} · {r['status']} · {r['created_at']}\n"
            f"👤 {r['username']}\n"
            f"✉️ {r['email']}\n"
            f"🔑 {r['password']}\n"
            f"📱 {r['phone']}"
        )
        if r["proxy"]:
            line += f"\n🌐 {mask_proxy_display(r['proxy'])}"
        if r["screenshot"]:
            line += f"\n🖼 {r['screenshot']}"
        if r["notes"]:
            line += f"\n📝 {r['notes']}"
        lines.append(line)

    # Telegram caps messages at ~4096 chars; chunk to stay safely under that.
    chunks, chunk = [], ""
    for line in lines:
        piece = line + "\n\n"
        if chunk and len(chunk) + len(piece) > 3500:
            chunks.append(chunk)
            chunk = ""
        chunk += piece
    if chunk:
        chunks.append(chunk)

    for c in chunks:
        await update.message.reply_text(c.strip())


@require_role(is_master)
async def photo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /photo <id> — see /list for ids.")
        return
    row_id = int(context.args[0])
    cur = conn.execute(
        f"SELECT {', '.join(db.COLUMNS)} FROM accounts WHERE id = ?", (row_id,)
    )
    row = cur.fetchone()
    if not row:
        await update.message.reply_text(f"No account with id {row_id}.")
        return
    r = dict(zip(db.COLUMNS, row))
    await send_result_photo(update, r["screenshot"], build_caption(r))


@require_role(is_master)
async def export_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/export defaults to successful signups only. /export all exports
    every status. N (a row limit), a status word, and a site URL can all be
    given, in any order, e.g. /export 50, /export all 50, /export failed,
    /export https://example.com, /export https://example.com failed 20."""
    limit = None
    status = "success"
    url = None
    for arg in context.args:
        if arg.isdigit():
            limit = max(1, min(int(arg), 5000))
        elif arg.startswith("http://") or arg.startswith("https://"):
            url = arg
        elif arg.lower() == "all":
            status = None
        else:
            status = arg
    filename = f"accounts_{status}.csv" if status else "accounts.csv"
    await send_csv(update, filename, limit=limit, status=status, url=url)


def _resolve_lane(chat_id, text):
    """Pick which lane a plain-text reply belongs to. Returns
    (session, rest_of_text, error_message) -- error_message is set (and the
    other two meaningless) if the reply couldn't be routed."""
    chat_sessions = sessions.get(chat_id)
    if not chat_sessions:
        return None, text, "No signup in progress. Send /newacc to start one."
    if len(chat_sessions) == 1:
        return next(iter(chat_sessions.values())), text, None
    # Multiple lanes active in this chat -- require a leading "<lane> " prefix
    # to disambiguate which one this phone/OTP is for.
    parts = text.split(maxsplit=1)
    if len(parts) == 2 and parts[0].isdigit() and int(parts[0]) in chat_sessions:
        return chat_sessions[int(parts[0])], parts[1].strip(), None
    active = ", ".join(str(i) for i in sorted(chat_sessions))
    return None, text, (f"Multiple signups in progress (lane(s) {active}). Prefix your "
                         "reply with the lane number, e.g. \"1 9876543210\".")


def _pop_session(chat_id, sub_id):
    chat_sessions = sessions.get(chat_id)
    if not chat_sessions:
        return
    chat_sessions.pop(sub_id, None)
    if not chat_sessions:
        del sessions[chat_id]


@require_role(is_admin)
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    raw_text = (update.message.text or "").strip()
    session, text, error = _resolve_lane(chat_id, raw_text)
    if error:
        await update.message.reply_text(error)
        return
    sub_id = session.sub_id
    loop = asyncio.get_running_loop()

    if session.stage == "await_phone":
        if not _valid_phone(text):
            await update.message.reply_text(f"⚠️ [#{sub_id}] That doesn't look like a valid phone "
                                             "number (digits only, 7-15 characters). Try again.")
            return
        session.acct["phone"] = text
        session.acct["proxy"] = session.proxy
        session.acct["url"] = session.site_url
        session.acct["referral_code"] = extract_referral_code(session.site_url or BOT_SITE_URL)
        session.row_id = db.insert_account(conn, session.acct)
        await update.message.reply_text(f"⏳ [#{sub_id}] Submitting the signup form and "
                                         "requesting an OTP, one moment…")

        logger.info(f"#{session.row_id} {session.acct['username']}: submitting "
                    f"(phone {text}, url {session.site_url or BOT_SITE_URL}, "
                    f"proxy {mask_proxy_display(session.proxy) if session.proxy else 'none'}, "
                    f"fast={session.use_fast})")
        if session.use_fast:
            # No Playwright involved -- run on asyncio's default executor
            # instead of a _pw_executors[slot], so this doesn't queue behind
            # (or block) browser-based signups sharing that slot.
            result = await loop.run_in_executor(None, _blocking_http_register, session, text)
        else:
            result = await loop.run_in_executor(
                _pw_executors[session.slot], _blocking_fill_and_register, session, text)

        if result.get("phone_taken"):
            logger.warning(f"#{session.row_id} {session.acct['username']}: phone taken -- {result['message']}")
            db.update_status(conn, session.row_id, "phone_taken", notes=result["message"])
            await end_session(session)
            await update.message.reply_text(f"⚠️ [#{sub_id}] {result['message']}")
            _pop_session(chat_id, sub_id)
            if (chat_id, sub_id) in looping_chats:
                await begin_signup(update, chat_id, sub_id)
            return

        if not result["ok"]:
            # Failure: just a plain confirmation, no photo/caption/CSV -- the
            # real error is logged to the console and stored in accounts.db
            # (notes + screenshot columns), retrievable via /list or /export,
            # rather than pushed into the chat (which would mean sending the
            # account's username/email/password/phone/proxy on every failure).
            logger.error(f"#{session.row_id} {session.acct['username']}: FAILED -- "
                         f"{result['message']} (screenshot: {result.get('shot')})")
            db.update_status(conn, session.row_id, "failed", notes=result["message"],
                             screenshot=result.get("shot"))
            await update.message.reply_text(f"❌ [#{sub_id}] Signup failed. (#{session.row_id})")
            await end_session(session)
            _pop_session(chat_id, sub_id)
            if (chat_id, sub_id) in looping_chats:
                await begin_signup(update, chat_id, sub_id)
            return

        logger.info(f"#{session.row_id} {session.acct['username']}: OTP screen reached")
        session.stage = "await_otp"
        await update.message.reply_text(
            f"📩 [#{sub_id}] OTP sent to {text}. Send the {result['digits']}-digit code you received."
        )

    elif session.stage == "await_otp":
        if not text.isdigit():
            await update.message.reply_text(f"⚠️ [#{sub_id}] Send just the numeric OTP code.")
            return

        if session.use_fast:
            result = await loop.run_in_executor(None, _blocking_http_verify_otp, session, text)
        else:
            result = await loop.run_in_executor(
                _pw_executors[session.slot], _blocking_verify_otp, session, text)

        status = "success" if result["ok"] else "failed"
        if result["ok"]:
            logger.info(f"#{session.row_id} {session.acct['username']}: SUCCESS")
        else:
            logger.error(f"#{session.row_id} {session.acct['username']}: OTP verify FAILED -- "
                         f"{result['message']} (screenshot: {result.get('shot')})")
        db.update_status(conn, session.row_id, status,
                         notes=result["message"], screenshot=result.get("shot"))

        if result["ok"]:
            # Success: just a plain confirmation, no photo/caption/CSV -- the
            # account details stay in accounts.db, retrievable via /list or
            # /export later, but aren't pushed into the chat automatically.
            await update.message.reply_text(f"✅ [#{sub_id}] Signup successful! (#{session.row_id})")
        else:
            # Failure: same policy as a register failure above -- no
            # photo/caption/CSV in chat, error logged + stored in accounts.db.
            await update.message.reply_text(f"❌ [#{sub_id}] Signup failed. (#{session.row_id})")

        await end_session(session)
        _pop_session(chat_id, sub_id)
        if (chat_id, sub_id) in looping_chats:
            await begin_signup(update, chat_id, sub_id)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Not role-gated -- shows role-appropriate content instead of a blanket
    rejection, since an unauthorized user still needs to see their own ID."""
    if update.effective_user is None:
        return
    user_id = update.effective_user.id
    # Set this user's "/" menu on first contact too, not just in post_init():
    # a freshly-created bot instance gets "Chat not found" at startup for
    # every master/admin who hasn't opened a chat with it yet, and without
    # this the menu would stay empty until the next process restart.
    menu = MASTER_COMMANDS if is_master(user_id) else ADMIN_COMMANDS if is_admin(user_id) else None
    if menu is not None:
        try:
            await context.bot.set_my_commands(menu, scope=BotCommandScopeChat(chat_id=user_id))
        except Exception as e:
            logger.warning(f"Could not set command menu for {user_id} on /start: {e}")
    signup_admin_help = (
        f"/newacc [count] — start continuous test signups (asks phone → OTP, then "
        f"auto-starts the next). count runs that many in parallel (1-{MAX_PARALLEL_NEWACC}); "
        "reply \"<lane> <phone/OTP>\" when more than one is active\n"
        "/done [lane] — stop after the current signup finishes\n"
        "/cancel [lane] — abandon in-progress signup(s)"
    )
    if is_master(user_id):
        sections = []
        if SIGNUP_ENABLED:
            sections.append("📝 Signups\n" + signup_admin_help)
            sections.append(
                "📊 Data\n"
                "/list [N] — recent stored accounts\n"
                "/stats [btag] — counts by status (and btag)\n"
                "/photo <id> — resend an account's screenshot\n"
                "/export [N] [status] [url] — CSV (successful by default; 'all' for every status)"
            )
        if HEDGE_ENABLED:
            hedge = [
                f"🎰 {'Stock Market' if STOCKMARKET_ENABLED else 'Casino'} / hedge betting",
                f"/pair <u1> <p1> <u2> <p2> — create a pair "
                f"(acc1 {RUN_GAME.side_a_label}, acc2 {RUN_GAME.side_b_label})",
                "/pairs — list pairs   ·   /delpair <id> — remove one",
                "/run <pair> <amount> <rounds> — run the hedge   ·   /stoprun — halt it",
                "/runs [pair] — run history   ·   /runlog <run_id> — round-by-round",
            ]
            if GAMEPLAY_ENABLED:
                hedge.append("/testbaccarat <user> <pass> [amount] — single-account bet test")
            if STOCKMARKET_ENABLED:
                hedge.append("Table minimum is ₹10 — start with /run <pair> 10 1.")
            sections.append("\n".join(hedge))
        settings_lines = ["⚙️ Settings (global)"]
        if SIGNUP_ENABLED:
            settings_lines.append("/setpassword <pw> | --random   ·   /password")
            settings_lines.append("/fast on|off — HTTP-fast signup mode (no browser, cricmatch only)")
        settings_lines.append("/setproxy <proxy> · /proxy · /clearproxy · /testproxy [proxy]")
        if SIGNUP_ENABLED:
            settings_lines.append("/seturl <url> · /url · /clearurl · /btag [code]")
        sections.append("\n".join(settings_lines))
        sections.append("👥 Admins\n/addadmin <id> · /removeadmin <id> · /admins")
        await update.message.reply_text(
            "🤖 Master admin — commands\n"
            "━━━━━━━━━━━━━━\n"
            + "\n\n".join(sections)
        )
    elif is_admin(user_id):
        if SIGNUP_ENABLED:
            await update.message.reply_text(
                "🤖 Admin — commands\n"
                "━━━━━━━━━━━━━━\n"
                + signup_admin_help
            )
        else:
            await update.message.reply_text(
                "🤖 This bot instance only runs casino/gameplay commands, "
                "which are master-only — there is nothing for admins here. "
                "Use the signup bot for /newacc."
            )
    else:
        await update.message.reply_text(
            "🔒 You are not authorized to use this bot.\n"
            f"Your Telegram user ID: {user_id}\n"
            "Share this with the master admin to request access."
        )


@require_role(is_master)
async def addadmin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /addadmin <telegram_user_id>")
        return
    new_id = context.args[0]
    if new_id in admin_ids:
        await update.message.reply_text(f"{new_id} is already an admin.")
        return
    admin_ids.add(new_id)
    save_admin_ids()
    try:
        await context.bot.set_my_commands(ADMIN_COMMANDS,
                                          scope=BotCommandScopeChat(chat_id=int(new_id)))
    except Exception as e:
        logger.warning(f"Could not set command menu for new admin {new_id}: {e}")
    await update.message.reply_text(f"✅ Added admin: {new_id}")


@require_role(is_master)
async def removeadmin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /removeadmin <telegram_user_id>")
        return
    old_id = context.args[0]
    if old_id not in admin_ids:
        await update.message.reply_text(f"{old_id} is not an admin.")
        return
    admin_ids.discard(old_id)
    save_admin_ids()
    try:
        await context.bot.delete_my_commands(scope=BotCommandScopeChat(chat_id=int(old_id)))
    except Exception as e:
        logger.warning(f"Could not clear command menu for removed admin {old_id}: {e}")
    await update.message.reply_text(f"✅ Removed admin: {old_id}")


@require_role(is_master)
async def list_admins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not admin_ids:
        await update.message.reply_text("No admins yet. Use /addadmin <user_id> to add one.")
        return
    await update.message.reply_text("👥 Admins\n━━━━━━━━━━━━━━\n" + "\n".join(f"• {a}" for a in sorted(admin_ids)))


async def on_error(update, context):
    """Global error handler. Transient long-poll network hiccups (ReadError,
    Bad Gateway, timeouts) are expected and auto-retried by PTB -- log them as a
    single WARNING line instead of the full 'No error handlers are registered'
    traceback that was tripping the log monitor. Anything else is a real bug and
    gets logged with its traceback."""
    err = context.error
    if isinstance(err, (NetworkError, TimedOut)):
        logger.warning("Transient Telegram network error (auto-retried): %s", err)
        return
    logger.error("Unhandled exception in handler:", exc_info=err)


def main():
    if not BOT_TOKEN:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN in .env (see .env.example) before running this bot.")
    if not MASTER_ADMIN_IDS:
        raise SystemExit("Set MASTER_ADMIN_ID in .env (see .env.example) before running this bot.")
    # concurrent_updates: PTB defaults to processing one update at a time, which
    # meant a quick command like /stoprun sat queued -- unanswered -- for the
    # entire duration of an in-flight /run (a single handler invocation that
    # awaits the whole multi-round run). Reproduced live: two /stoprun sends
    # during round 2/4 of a real run both landed only after round 4 finished.
    # The actual Playwright work stays serialized per-slot via _pw_executors
    # regardless, so allowing concurrent update dispatch just lets control
    # commands (/stoprun, /done, /cancel) interrupt promptly.
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).concurrent_updates(True).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    if SIGNUP_ENABLED:
        app.add_handler(CommandHandler("newacc", newacc))
        app.add_handler(CommandHandler("cancel", cancel))
        app.add_handler(CommandHandler("done", done_cmd))
        app.add_handler(CommandHandler("stats", stats))
        app.add_handler(CommandHandler("setpassword", setpassword))
        app.add_handler(CommandHandler("password", show_password))
        app.add_handler(CommandHandler("fast", fast_cmd))
        app.add_handler(CommandHandler("list", list_accounts))
        app.add_handler(CommandHandler("photo", photo_cmd))
        app.add_handler(CommandHandler("export", export_cmd))
        app.add_handler(CommandHandler("seturl", seturl))
        app.add_handler(CommandHandler("url", show_url))
        app.add_handler(CommandHandler("clearurl", clearurl))
        app.add_handler(CommandHandler("btag", btag_cmd))
        # Phone/OTP replies only exist for signup sessions -- a gameplay-only
        # instance has no sessions, so plain text is just ignored there.
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    if GAMEPLAY_ENABLED:
        app.add_handler(CommandHandler("testbaccarat", testbaccarat))
    if HEDGE_ENABLED:
        app.add_handler(CommandHandler("pair", cpair))
        app.add_handler(CommandHandler("pairs", pairs_cmd))
        app.add_handler(CommandHandler("delpair", delpair))
        app.add_handler(CommandHandler("run", run_cmd))
        app.add_handler(CommandHandler("stoprun", stoprun))
        app.add_handler(CommandHandler("runs", runs_cmd))
        app.add_handler(CommandHandler("runlog", runlog_cmd))
    app.add_handler(CommandHandler("setproxy", setproxy))
    app.add_handler(CommandHandler("proxy", show_proxy))
    app.add_handler(CommandHandler("clearproxy", clearproxy))
    app.add_handler(CommandHandler("testproxy", testproxy))
    app.add_handler(CommandHandler("addadmin", addadmin))
    app.add_handler(CommandHandler("removeadmin", removeadmin))
    app.add_handler(CommandHandler("admins", list_admins))
    app.add_error_handler(on_error)

    # Both modes still need the shared slot browsers: signups obviously, and
    # gameplay's /testbaccarat + /testproxy run on slot 0 (each /run launches
    # its own temporary browsers regardless).
    logger.info(f"Warming up {BOT_CONCURRENCY} browser slot(s)...")
    for slot in range(BOT_CONCURRENCY):
        _pw_executors[slot].submit(_blocking_ensure_browser, slot).result()

    logger.info(f"Bot starting... env={_env_file} mode={BOT_MODE} site={BOT_SITE_URL} "
                f"concurrency={BOT_CONCURRENCY} "
                f"admins_file={ADMINS_FILE} settings_file={SETTINGS_FILE} "
                f"pairs_file={PAIRS_FILE} pair_runs_file={PAIR_RUNS_FILE}")
    try:
        # bootstrap_retries=-1: keep retrying the startup get_me() on transient
        # network errors instead of aborting the whole process if Telegram is
        # momentarily unreachable at boot (a bad token still fails fast -- that's
        # an auth error, not a network error, so it isn't retried).
        app.run_polling(bootstrap_retries=-1)
    finally:
        for slot in range(BOT_CONCURRENCY):
            _pw_executors[slot].submit(_blocking_shutdown_browser, slot).result()
            _pw_executors[slot].shutdown(wait=False)
        # No persistent browser to close here -- each /run launches and closes
        # its own temporary Banker/Player browsers inside run_paired_hedge.
        _run_executor.shutdown(wait=False)


if __name__ == "__main__":
    main()
