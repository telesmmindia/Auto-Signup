"""
Telegram bot wrapper for the QA signup driver.

Flow:
    /newacc  -> bot generates a random test identity, asks for a phone number
    (you send the phone number)
    -> bot fills + submits the signup form, asks for the OTP sent by SMS
    (you send the OTP)
    -> bot verifies it and replies with the result screenshot, captioned with
       the full signup details (username/email/password/phone/proxy/status)

Other commands:
    /stats            -> counts of signups by status
    /list [N]         -> most recent N stored accounts (default 10)
    /photo <id>       -> resend a stored account's screenshot with its
                         details as the caption (id from /list)
    /cancel           -> abandon an in-progress signup
    /setproxy <proxy> -> set the proxy used for this chat's future signups
                         (host:port, host:port:username:password, or a URL)
    /proxy            -> show the currently set proxy
    /clearproxy       -> stop using a proxy (direct connection)
    /testproxy [proxy] -> open the proxy in a browser context and report the
                         exit IP; tests the saved proxy if none is given
    /seturl <url>     -> use a different site for this chat's future signups
    /url              -> show the currently set site URL
    /clearurl         -> reset to the default site URL

Setup:
    cp .env.example .env
    # edit .env and paste your token from @BotFather into TELEGRAM_BOT_TOKEN
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
import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, Error as PWError
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

import db
from main import (
    SEL, SHOTS_DIR, SITE_URL,
    check_phone_taken, click_first_visible, gen_account, maybe_bridge_proxy,
    open_signup_modal, parse_proxy, read_result, stop_bridge,
    wait_for_otp_outcome, wait_for_register_outcome,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

load_dotenv()
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")

# Shared across handlers; all handler coroutines run on the same asyncio event
# loop thread, so one sqlite3 connection is safe to reuse.
conn = db.get_connection()

# chat_id -> Session, for signups currently in progress.
sessions = {}

# chat_id (str) -> raw proxy string, persisted across restarts. Gitignored
# since a proxy string commonly embeds credentials.
PROXY_FILE = Path("proxy_settings.json")
# chat_id (str) -> site URL override, persisted across restarts.
URL_FILE = Path("url_settings.json")


def _load_json_dict(path):
    if path.exists():
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_json_dict(path, data):
    path.write_text(json.dumps(data))


chat_proxies = _load_json_dict(PROXY_FILE)
chat_urls = _load_json_dict(URL_FILE)


def save_chat_proxies():
    _save_json_dict(PROXY_FILE, chat_proxies)


def save_chat_urls():
    _save_json_dict(URL_FILE, chat_urls)


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
        return {"ok": False, "message": "Could not open the signup modal (JOIN button)."}

    for sel, value in [(SEL["username"], acct["username"]),
                       (SEL["email"], acct["email"]),
                       (SEL["password"], acct["password"]),
                       (SEL["phone"], str(phone))]:
        field = page.locator(sel)
        field.click()
        field.press_sequentially(value, delay=30)
        field.blur()

    try:
        cb = page.locator(SEL["terms"])
        if cb.count() and not cb.is_checked():
            cb.check(force=True)
    except Exception:
        pass

    SHOTS_DIR.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    page.screenshot(path=str(SHOTS_DIR / f"{acct['username']}-{stamp}-filled.png"))

    page.click(SEL["submit"])
    outcome = wait_for_register_outcome(page)

    result_shot = SHOTS_DIR / f"{acct['username']}-{stamp}-result.png"
    page.screenshot(path=str(result_shot))

    if outcome == "phone_taken":
        return {"ok": False, "phone_taken": True, "message": check_phone_taken(page),
                "shot": str(result_shot)}

    if outcome in ("error", "timeout"):
        msgs = read_result(page)
        return {"ok": False, "message": "Register rejected: " + ("; ".join(msgs) or "unknown error"),
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


async def newacc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id in sessions:
        await update.message.reply_text(
            "You already have a signup in progress. Reply with the phone "
            "number/OTP it's waiting for, or send /cancel."
        )
        return

    session = Session()
    session.acct = gen_account()
    session.stage = "await_phone"
    session.proxy = chat_proxies.get(str(chat_id))
    session.site_url = chat_urls.get(str(chat_id))
    sessions[chat_id] = session

    proxy_line = (f"Proxy: {mask_proxy_display(session.proxy)}" if session.proxy
                 else "Proxy: none (direct connection) — use /setproxy to add one.")
    url_line = f"URL: {session.site_url or SITE_URL}"
    await update.message.reply_text(
        "New test signup:\n"
        f"Username: {session.acct['username']}\n"
        f"Email: {session.acct['email']}\n"
        f"{proxy_line}\n"
        f"{url_line}\n\n"
        "Send the phone number to use for this signup."
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    session = sessions.pop(chat_id, None)
    if session:
        await end_session(session)
        await update.message.reply_text("Cancelled the in-progress signup.")
    else:
        await update.message.reply_text("No signup in progress.")


async def setproxy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
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
    chat_proxies[chat_id] = raw
    save_chat_proxies()
    await update.message.reply_text(
        f"Proxy set for future signups: {mask_proxy_display(raw)}\n"
        "Tip: /testproxy to confirm it works before running /newacc."
    )


async def show_proxy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    raw = chat_proxies.get(chat_id)
    if raw:
        await update.message.reply_text(f"Current proxy: {mask_proxy_display(raw)}")
    else:
        await update.message.reply_text("No proxy set — signups use a direct connection.")


async def clearproxy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    if chat_proxies.pop(chat_id, None) is not None:
        save_chat_proxies()
        await update.message.reply_text("Proxy cleared — signups will use a direct connection.")
    else:
        await update.message.reply_text("No proxy was set.")


async def seturl(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    if not context.args:
        await update.message.reply_text("Usage: /seturl https://example.com")
        return
    url = context.args[0]
    if not (url.startswith("http://") or url.startswith("https://")):
        await update.message.reply_text("URL must start with http:// or https://")
        return
    chat_urls[chat_id] = url
    save_chat_urls()
    await update.message.reply_text(f"Site URL set for future signups: {url}")


async def show_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    url = chat_urls.get(chat_id)
    await update.message.reply_text(f"Current site URL: {url or SITE_URL}"
                                    + ("" if url else " (default)"))


async def clearurl(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    if chat_urls.pop(chat_id, None) is not None:
        save_chat_urls()
        await update.message.reply_text(f"Site URL reset to default: {SITE_URL}")
    else:
        await update.message.reply_text("No custom site URL was set.")


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


async def testproxy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    raw = context.args[0] if context.args else chat_proxies.get(chat_id)
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


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cur = conn.execute("SELECT status, COUNT(*) FROM accounts GROUP BY status")
    rows = cur.fetchall()
    total = sum(c for _, c in rows)
    lines = [f"Total signups recorded: {total}"]
    for status, count in rows:
        lines.append(f"  {status}: {count}")
    await update.message.reply_text("\n".join(lines) if rows else "No signups recorded yet.")


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
        session.row_id = db.insert_account(conn, session.acct)
        await update.message.reply_text("Submitting the signup form and requesting an OTP, one moment...")

        result = await loop.run_in_executor(
            _pw_executor, _blocking_fill_and_register, session, text)

        if result.get("phone_taken"):
            db.update_status(conn, session.row_id, "phone_taken", notes=result["message"])
            await close_browser(session)
            await update.message.reply_text(
                f"{result['message']} Send a different phone number to try again, or /cancel."
            )
            return  # stays in "await_phone" stage, session kept alive

        if not result["ok"]:
            db.update_status(conn, session.row_id, "failed", notes=result["message"],
                             screenshot=result.get("shot"))
            acct = dict(session.acct)
            acct.update(status="failed", notes=f"Signup failed: {result['message']}")
            await send_result_photo(update, result.get("shot"), build_caption(acct))
            await end_session(session)
            del sessions[chat_id]
            return

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
        db.update_status(conn, session.row_id, status,
                         notes=result["message"], screenshot=result.get("shot"))

        acct = dict(session.acct)
        acct.update(status=status,
                    notes=("Signup successful!" if result["ok"]
                          else f"Verification failed: {result['message']}"))
        await send_result_photo(update, result.get("shot"), build_caption(acct))

        await end_session(session)
        del sessions[chat_id]


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Commands:\n"
        "/newacc - start a new test signup (asks for phone, then OTP)\n"
        "/stats - counts of signups by status\n"
        "/list [N] - most recent N stored accounts\n"
        "/photo <id> - resend a stored account's screenshot with its details as caption\n"
        "/cancel - abandon an in-progress signup\n"
        "/setproxy <proxy> - use a proxy for this chat's future signups\n"
        "/proxy - show the currently set proxy\n"
        "/clearproxy - stop using a proxy\n"
        "/testproxy [proxy] - check a proxy actually works\n"
        "/seturl <url> - use a different site for this chat's future signups\n"
        "/url - show the currently set site URL\n"
        "/clearurl - reset to the default site URL"
    )


def main():
    if not BOT_TOKEN:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN in .env (see .env.example) before running this bot.")
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(CommandHandler("newacc", newacc))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("list", list_accounts))
    app.add_handler(CommandHandler("photo", photo_cmd))
    app.add_handler(CommandHandler("setproxy", setproxy))
    app.add_handler(CommandHandler("proxy", show_proxy))
    app.add_handler(CommandHandler("clearproxy", clearproxy))
    app.add_handler(CommandHandler("testproxy", testproxy))
    app.add_handler(CommandHandler("seturl", seturl))
    app.add_handler(CommandHandler("url", show_url))
    app.add_handler(CommandHandler("clearurl", clearurl))
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
