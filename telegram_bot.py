"""
Telegram bot wrapper for the QA signup driver.

Two roles:
    master admin -> exactly one, set via MASTER_ADMIN_ID in .env. Can do
                    everything: create/remove admins, set the GLOBAL proxy
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
"""
import asyncio
import functools
import json
import logging
import os
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, Error as PWError
from telegram import BotCommand, BotCommandScopeChat, BotCommandScopeDefault, Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

import db
from main import (
    SEL, SHOTS_DIR, SITE_URL,
    capsolver_key, check_phone_taken, click_first_visible, extract_referral_code,
    fill_register_form, gen_account, is_waf_captcha, maybe_bridge_proxy,
    open_signup_modal, parse_proxy, read_result, stop_bridge, submit_register,
    wait_for_otp_outcome, wait_for_register_outcome,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

load_dotenv()
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
# The one, fixed top-level role. Set via .env, not changeable from the bot
# itself -- a compromised admin session should never be able to promote
# itself to master.
MASTER_ADMIN_ID = os.environ.get("MASTER_ADMIN_ID")

# Shared across handlers; all handler coroutines run on the same asyncio event
# loop thread, so one sqlite3 connection is safe to reuse.
conn = db.get_connection()

# chat_id -> Session, for signups currently in progress.
sessions = {}
# chat_ids currently in continuous mode: a new signup auto-starts after each
# one finishes, until /done or /cancel removes the chat_id from this set.
looping_chats = set()

# Telegram user IDs (str) the master admin has authorized, persisted across
# restarts. Gitignored -- this is access-control state, not project content.
ADMINS_FILE = Path("admins.json")
# Proxy/URL are GLOBAL (master-controlled, apply to every admin's signups),
# not per-chat -- {"proxy": "...", "url": "..."}.
SETTINGS_FILE = Path("bot_settings.json")


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


def save_admin_ids():
    _save_json(ADMINS_FILE, sorted(admin_ids))


def save_settings():
    _save_json(SETTINGS_FILE, global_settings)


def is_master(user_id):
    return MASTER_ADMIN_ID is not None and str(user_id) == str(MASTER_ADMIN_ID)


def is_admin(user_id):
    return is_master(user_id) or str(user_id) in admin_ids


def require_role(check):
    """Decorator gating a handler to users satisfying `check(user_id)`. On
    denial, replies with the user's own Telegram ID so they can hand it to
    the master admin for /addadmin."""
    def decorator(func):
        @functools.wraps(func)
        async def wrapper(update, context):
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
ADMIN_COMMANDS = [
    BotCommand("newacc", "Start continuous test signups"),
    BotCommand("done", "Stop continuous signups after the current one"),
    BotCommand("cancel", "Abandon an in-progress signup"),
    BotCommand("start", "Show available commands"),
]
MASTER_COMMANDS = ADMIN_COMMANDS + [
    BotCommand("list", "Recent stored accounts"),
    BotCommand("photo", "Resend a stored account's screenshot"),
    BotCommand("export", "Export accounts as a CSV file"),
    BotCommand("stats", "Counts of signups by status and btag"),
    BotCommand("setpassword", "Set a fixed password for all signups, or --random"),
    BotCommand("password", "Show the current password mode"),
    BotCommand("setproxy", "Set the global proxy for all signups"),
    BotCommand("proxy", "Show the global proxy"),
    BotCommand("clearproxy", "Clear the global proxy"),
    BotCommand("testproxy", "Check a proxy actually works"),
    BotCommand("seturl", "Set the global site URL for all signups"),
    BotCommand("url", "Show the global site URL"),
    BotCommand("clearurl", "Reset to the default site URL"),
    BotCommand("btag", "Set/show just the btag on the global site URL"),
    BotCommand("addadmin", "Authorize a new admin"),
    BotCommand("removeadmin", "Revoke an admin"),
    BotCommand("admins", "List current admins"),
]


async def post_init(application):
    """Runs once at startup, before polling begins: empties the default
    command menu, then gives the master and every already-authorized admin
    their role-appropriate menu."""
    bot = application.bot
    await bot.set_my_commands([], scope=BotCommandScopeDefault())
    if MASTER_ADMIN_ID:
        try:
            await bot.set_my_commands(MASTER_COMMANDS,
                                      scope=BotCommandScopeChat(chat_id=int(MASTER_ADMIN_ID)))
        except Exception as e:
            logger.warning(f"Could not set master command menu: {e}")
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


# One Chromium process for the bot's whole lifetime, plus the single worker
# thread all Playwright calls must run on (sync API is thread-affine). Each
# session gets its own BrowserContext (isolated cookies/storage) opened from
# this already-running browser, instead of launching a new process every time.
_pw_executor = ThreadPoolExecutor(max_workers=1)
_playwright = None
_browser = None


class Session:
    def __init__(self):
        self.context = None
        self.page = None
        self.acct = None
        self.row_id = None
        self.stage = None  # "await_phone" | "await_otp"
        self.proxy = None  # raw proxy string, or None for a direct connection
        self.bridge_proc = None  # local pproxy process, if the proxy needed one
        self.site_url = None  # site URL for this signup (falls back to SITE_URL)


def _valid_phone(text):
    return text.isdigit() and 7 <= len(text) <= 15


def _blocking_ensure_browser():
    """Launch the shared Chromium instance once; reused by every session."""
    global _playwright, _browser
    if _browser is None:
        _playwright = sync_playwright().start()
        _browser = _playwright.chromium.launch(headless=True)
    return _browser


def _blocking_shutdown_browser():
    global _playwright, _browser
    try:
        if _browser:
            _browser.close()
    except Exception:
        pass
    try:
        if _playwright:
            _playwright.stop()
    except Exception:
        pass
    _browser = None
    _playwright = None


def _blocking_close_context(session):
    """Must run on _pw_executor's worker thread -- Playwright's sync API
    requires teardown to happen on the same thread the object was created on."""
    try:
        if session.context:
            session.context.close()
    except Exception:
        pass
    session.context = None
    session.page = None
    stop_bridge(session.bridge_proc)
    session.bridge_proc = None


async def close_browser(session):
    """Tear down just this session's browser context (keeping the shared
    Chromium process running) so a retry (e.g. 'phone already taken') is fast."""
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(_pw_executor, _blocking_close_context, session)


async def end_session(session):
    """Alias kept for call-site clarity; the shared browser process itself
    outlives any single session and is only shut down when the bot exits."""
    await close_browser(session)


def _blocking_fill_and_register(session, phone):
    """Runs on _pw_executor's single worker thread. Opens a fresh browser
    context on the shared browser, fills the form, submits, and waits for the
    OTP screen."""
    browser = _blocking_ensure_browser()
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
        page.goto(session.site_url or SITE_URL, wait_until="domcontentloaded", timeout=60000)
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
    # CAPTCHA-blocked and CAPSOLVER_API_KEY is set, solves it (via CapSolver),
    # injects the token, and resubmits once -- all shared with the CLI. Without
    # a key it's a plain submit and `captured` still lets us report the block.
    outcome, msgs, captured = submit_register(page, context, acct, session.site_url,
                                              proxy=session.proxy)

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

    digits = page.locator(SEL["otp_digits"]).count()
    if digits == 0:
        return {"ok": False, "message": "OTP screen detected but no digit inputs found.",
                "shot": str(result_shot)}
    return {"ok": True, "digits": digits, "shot": str(result_shot)}


def _blocking_verify_otp(session, otp):
    """Runs on the SAME worker thread as _blocking_fill_and_register."""
    page = session.page
    acct = session.acct
    boxes = page.locator(SEL["otp_digits"])
    for i, ch in enumerate(otp):
        box = boxes.nth(i)
        box.click()
        box.press_sequentially(ch, delay=40)
    page.wait_for_timeout(500)

    stamp = time.strftime("%Y%m%d-%H%M%S")
    otp_filled = SHOTS_DIR / f"{acct['username']}-{stamp}-otp-filled.png"
    page.screenshot(path=str(otp_filled))

    if not click_first_visible(page, SEL["otp_verify"], timeout=6000):
        return {"ok": False, "message": "Could not find a visible Verify button.", "shot": str(otp_filled)}
    outcome = wait_for_otp_outcome(page)

    otp_result = SHOTS_DIR / f"{acct['username']}-{stamp}-otp-result.png"
    page.screenshot(path=str(otp_result))

    if outcome == "error":
        err = ""
        try:
            e = page.locator(SEL["otp_error"]).first
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


async def begin_signup(update, chat_id):
    """Generate a new account and start a fresh session. Shared by /newacc
    (the first one) and the continuous-mode auto-restart in handle_message()
    after a signup finishes."""
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
    sessions[chat_id] = session

    await update.message.reply_text("Send the phone number to use for this signup.")


@require_role(is_admin)
async def newacc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id in sessions:
        await update.message.reply_text(
            "You already have a signup in progress. Reply with the phone "
            "number/OTP it's waiting for, or send /cancel."
        )
        return

    # /newacc now runs continuously: after each signup finishes, a new one
    # starts automatically until /done (or /cancel) is sent.
    looping_chats.add(chat_id)
    await begin_signup(update, chat_id)


@require_role(is_admin)
async def done_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    was_looping = chat_id in looping_chats
    looping_chats.discard(chat_id)
    if chat_id in sessions:
        await update.message.reply_text(
            "Will stop after the current signup finishes." if was_looping
            else "Not in continuous mode -- nothing to stop."
        )
    else:
        await update.message.reply_text(
            "Continuous mode stopped." if was_looping
            else "Not in continuous mode."
        )


@require_role(is_admin)
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    looping_chats.discard(chat_id)
    session = sessions.pop(chat_id, None)
    if session:
        await end_session(session)
        await update.message.reply_text("Cancelled the in-progress signup.")
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
            "Password mode: RANDOM — each new signup gets its own random password again."
        )
        return
    password = context.args[0]
    global_settings["password"] = password
    save_settings()
    await update.message.reply_text(
        f"Password mode: FIXED — every future signup will use: {password}\n"
        "Use /setpassword --random to go back to random passwords."
    )


@require_role(is_master)
async def show_password(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pw = global_settings.get("password")
    if pw:
        await update.message.reply_text(f"Current password mode: FIXED — {pw}")
    else:
        await update.message.reply_text("Current password mode: RANDOM (default, per-signup)")


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
        f"Global proxy set for all admins' signups: {mask_proxy_display(raw)}\n"
        "Tip: /testproxy to confirm it works before running /newacc."
    )


@require_role(is_master)
async def show_proxy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = global_settings.get("proxy")
    if raw:
        await update.message.reply_text(f"Current global proxy: {mask_proxy_display(raw)}")
    else:
        await update.message.reply_text("No proxy set — signups use a direct connection.")


@require_role(is_master)
async def clearproxy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if global_settings.pop("proxy", None) is not None:
        save_settings()
        await update.message.reply_text("Global proxy cleared — signups will use a direct connection.")
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
    await update.message.reply_text(f"Global site URL set for all admins' signups: {url}")


@require_role(is_master)
async def show_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = global_settings.get("url")
    await update.message.reply_text(f"Current global site URL: {url or SITE_URL}"
                                    + ("" if url else " (default)"))


@require_role(is_master)
async def clearurl(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if global_settings.pop("url", None) is not None:
        save_settings()
        await update.message.reply_text(f"Site URL reset to default: {SITE_URL}")
    else:
        await update.message.reply_text("No custom site URL was set.")


@require_role(is_master)
async def btag_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set (or show) just the btag affiliate/referral code, keeping whatever
    scheme/host/path the global site URL (or the SITE_URL default) already
    has -- so the master doesn't have to retype the whole URL to swap tags."""
    current_url = global_settings.get("url") or SITE_URL
    if not context.args:
        code = extract_referral_code(current_url)
        await update.message.reply_text(
            f"Current btag: {code}" if code else "No btag set on the current site URL."
        )
        return
    code = context.args[0]
    parts = urlsplit(current_url)
    new_url = urlunsplit((parts.scheme, parts.netloc, parts.path, f"btag={code}", parts.fragment))
    global_settings["url"] = new_url
    save_settings()
    await update.message.reply_text(f"Global site URL set for all admins' signups: {new_url}")


def _blocking_test_proxy_once(proxy_conf, timeout_ms=30000):
    browser = _blocking_ensure_browser()
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
    """Runs on _pw_executor. Opens a throwaway context with the given proxy
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
    result = await loop.run_in_executor(_pw_executor, _blocking_test_proxy, proxy_conf)
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
        lines = [f"Signups for btag {btag}: {total}"]
        for status, count in rows:
            lines.append(f"  {status}: {count}")
        await update.message.reply_text("\n".join(lines))
        return

    cur = conn.execute("SELECT status, COUNT(*) FROM accounts GROUP BY status")
    rows = cur.fetchall()
    total = sum(c for _, c in rows)
    if not rows:
        await update.message.reply_text("No signups recorded yet.")
        return
    lines = [f"Total signups recorded: {total}", "", "By status:"]
    for status, count in rows:
        lines.append(f"  {status}: {count}")

    btag_rows = conn.execute(
        "SELECT COALESCE(referral_code, '(none)'), COUNT(*) FROM accounts "
        "GROUP BY referral_code ORDER BY COUNT(*) DESC"
    ).fetchall()
    lines.append("")
    lines.append("By btag:")
    for code, count in btag_rows:
        lines.append(f"  {code}: {count}")

    await update.message.reply_text("\n".join(lines))


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
            f"#{r['id']} [{r['status']}] {r['created_at']}\n"
            f"  username: {r['username']}\n"
            f"  email: {r['email']}\n"
            f"  password: {r['password']}\n"
            f"  phone: {r['phone']}"
        )
        if r["proxy"]:
            line += f"\n  proxy: {mask_proxy_display(r['proxy'])}"
        if r["screenshot"]:
            line += f"\n  screenshot: {r['screenshot']}"
        if r["notes"]:
            line += f"\n  notes: {r['notes']}"
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


@require_role(is_admin)
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    session = sessions.get(chat_id)
    if not session:
        await update.message.reply_text("No signup in progress. Send /newacc to start one.")
        return

    text = (update.message.text or "").strip()
    loop = asyncio.get_running_loop()

    if session.stage == "await_phone":
        if not _valid_phone(text):
            await update.message.reply_text("That doesn't look like a valid phone number "
                                             "(digits only, 7-15 characters). Try again.")
            return
        session.acct["phone"] = text
        session.acct["proxy"] = session.proxy
        session.acct["url"] = session.site_url
        session.acct["referral_code"] = extract_referral_code(session.site_url or SITE_URL)
        session.row_id = db.insert_account(conn, session.acct)
        await update.message.reply_text("Submitting the signup form and requesting an OTP, one moment...")

        logger.info(f"#{session.row_id} {session.acct['username']}: submitting "
                    f"(phone {text}, url {session.site_url or SITE_URL}, "
                    f"proxy {mask_proxy_display(session.proxy) if session.proxy else 'none'})")
        result = await loop.run_in_executor(
            _pw_executor, _blocking_fill_and_register, session, text)

        if result.get("phone_taken"):
            logger.warning(f"#{session.row_id} {session.acct['username']}: phone taken -- {result['message']}")
            db.update_status(conn, session.row_id, "phone_taken", notes=result["message"])
            await end_session(session)
            await update.message.reply_text(result["message"])
            del sessions[chat_id]
            if chat_id in looping_chats:
                await begin_signup(update, chat_id)
            return

        if not result["ok"]:
            logger.error(f"#{session.row_id} {session.acct['username']}: FAILED -- "
                         f"{result['message']} (screenshot: {result.get('shot')})")
            db.update_status(conn, session.row_id, "failed", notes=result["message"],
                             screenshot=result.get("shot"))
            acct = dict(session.acct)
            acct.update(status="failed", notes=f"Signup failed: {result['message']}")
            await send_result_photo(update, result.get("shot"), build_caption(acct))
            await send_csv(update, f"{acct['username']}.csv", row_id=session.row_id)
            await end_session(session)
            del sessions[chat_id]
            if chat_id in looping_chats:
                await begin_signup(update, chat_id)
            return

        logger.info(f"#{session.row_id} {session.acct['username']}: OTP screen reached")
        session.stage = "await_otp"
        await update.message.reply_text(
            f"OTP sent to {text}. Send the {result['digits']}-digit code you received."
        )

    elif session.stage == "await_otp":
        if not text.isdigit():
            await update.message.reply_text("Send just the numeric OTP code.")
            return

        result = await loop.run_in_executor(
            _pw_executor, _blocking_verify_otp, session, text)

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
            await update.message.reply_text(f"Signup successful! (#{session.row_id})")
        else:
            acct = dict(session.acct)
            acct.update(status=status, notes=f"Verification failed: {result['message']}")
            await send_result_photo(update, result.get("shot"), build_caption(acct))
            await send_csv(update, f"{session.acct['username']}.csv", row_id=session.row_id)

        await end_session(session)
        del sessions[chat_id]
        if chat_id in looping_chats:
            await begin_signup(update, chat_id)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Not role-gated -- shows role-appropriate content instead of a blanket
    rejection, since an unauthorized user still needs to see their own ID."""
    user_id = update.effective_user.id
    if is_master(user_id):
        await update.message.reply_text(
            "Commands (master admin):\n"
            "/newacc - start continuous test signups (asks for phone, then OTP, "
            "then starts the next one automatically)\n"
            "/done - stop after the current signup finishes\n"
            "/cancel - abandon an in-progress signup (also stops looping)\n"
            "/stats - counts of signups by status and by btag\n"
            "/stats <btag> - status breakdown for just that btag\n"
            "/list [N] - most recent N stored accounts\n"
            "/photo <id> - resend a stored account's screenshot with its details as caption\n"
            "/export [N] [status] [url] - CSV of successful signups by default; "
            "/export all for every status, /export failed for a specific one, "
            "/export https://example.com to filter by site URL\n"
            "/setpassword <pw> - fixed password for every future signup (--random to revert)\n"
            "/password - show the current password mode\n"
            "/setproxy <proxy> - set the GLOBAL proxy for every admin's signups\n"
            "/proxy - show the global proxy\n"
            "/clearproxy - clear the global proxy\n"
            "/testproxy [proxy] - check a proxy actually works\n"
            "/seturl <url> - set the GLOBAL site URL for every admin's signups\n"
            "/url - show the global site URL\n"
            "/clearurl - reset to the default site URL\n"
            "/btag <code> - set just the btag on the global site URL (keeps scheme/host/path)\n"
            "/btag - show the current btag\n"
            "/addadmin <user_id> - authorize a new admin\n"
            "/removeadmin <user_id> - revoke an admin\n"
            "/admins - list current admins"
        )
    elif is_admin(user_id):
        await update.message.reply_text(
            "Commands (admin):\n"
            "/newacc - start continuous test signups (asks for phone, then OTP, "
            "then starts the next one automatically)\n"
            "/done - stop after the current signup finishes\n"
            "/cancel - abandon an in-progress signup (also stops looping)"
        )
    else:
        await update.message.reply_text(
            "You are not authorized to use this bot.\n"
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
    await update.message.reply_text(f"Added admin: {new_id}")


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
    await update.message.reply_text(f"Removed admin: {old_id}")


@require_role(is_master)
async def list_admins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not admin_ids:
        await update.message.reply_text("No admins yet. Use /addadmin <user_id> to add one.")
        return
    await update.message.reply_text("Admins:\n" + "\n".join(sorted(admin_ids)))


def main():
    if not BOT_TOKEN:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN in .env (see .env.example) before running this bot.")
    if not MASTER_ADMIN_ID:
        raise SystemExit("Set MASTER_ADMIN_ID in .env (see .env.example) before running this bot.")
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(CommandHandler("newacc", newacc))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CommandHandler("done", done_cmd))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("setpassword", setpassword))
    app.add_handler(CommandHandler("password", show_password))
    app.add_handler(CommandHandler("list", list_accounts))
    app.add_handler(CommandHandler("photo", photo_cmd))
    app.add_handler(CommandHandler("export", export_cmd))
    app.add_handler(CommandHandler("setproxy", setproxy))
    app.add_handler(CommandHandler("proxy", show_proxy))
    app.add_handler(CommandHandler("clearproxy", clearproxy))
    app.add_handler(CommandHandler("testproxy", testproxy))
    app.add_handler(CommandHandler("seturl", seturl))
    app.add_handler(CommandHandler("url", show_url))
    app.add_handler(CommandHandler("clearurl", clearurl))
    app.add_handler(CommandHandler("btag", btag_cmd))
    app.add_handler(CommandHandler("addadmin", addadmin))
    app.add_handler(CommandHandler("removeadmin", removeadmin))
    app.add_handler(CommandHandler("admins", list_admins))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Warming up the shared browser...")
    _pw_executor.submit(_blocking_ensure_browser).result()

    logger.info("Bot starting...")
    try:
        app.run_polling()
    finally:
        _pw_executor.submit(_blocking_shutdown_browser).result()
        _pw_executor.shutdown(wait=False)


if __name__ == "__main__":
    main()
