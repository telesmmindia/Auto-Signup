"""
Instagram Account Creator — Telegram Bot Edition
================================================
Commands:
  /new       — start signup flow (bot asks for email → does signup → asks OTP)
  /accounts  — get all saved accounts
  /cancel    — abort current signup
  /help      — show commands

The bot runs the browser on THIS machine. You control it from Telegram.
"""
import json
import tempfile
import shutil

import csv
import logging
import os
import random
import re
import threading
import time
from datetime import datetime

import telebot
from DrissionPage import ChromiumPage, ChromiumOptions

# ═══════════════════════════════════════════════
#  CONFIG
# ═══════════════════════════════════════════════
BOT_TOKEN = "8874180830:AAEIKAuW8OauIhwtNRT-zX9u4qUPDiwOXSw"   # ← from @BotFather
HEADLESS = True              # False on your Mac for debugging, True on the VPS
PROXIES_FILE = "proxies.txt"   # one proxy per line; empty = use direct IP

ALLOWED_CHAT_IDS = []    # empty = anyone can use. Otherwise: [123456789]
                        # (get your ID by messaging @userinfobot)

ACCOUNTS_FILE = "accounts.csv"
USED_EMAILS_FILE = "emails_used.txt"
ERRORS_DIR = "errors"
LOG_FILE = "insta_bot.log"

# How long the bot waits for YOU to send the OTP before auto-cancelling (seconds)
OTP_TIMEOUT = 300        # 5 minutes

MAX_DELAY_JITTER = (2, 5)   # random extra delays to look human

# ═══════════════════════════════════════════════
#  INDIAN NAME DATA
# ═══════════════════════════════════════════════
FIRST_NAMES = [
    "Aarav", "Vivaan", "Aditya", "Vihaan", "Arjun", "Sai", "Reyansh",
    "Ayaan", "Krishna", "Ishaan", "Rohan", "Kabir", "Dhruv", "Rahul",
    "Aryan", "Vikram", "Ananya", "Diya", "Aadhya", "Kiara", "Myra",
    "Saanvi", "Pari", "Anika", "Navya", "Aisha", "Riya", "Meera",
    "Sanya", "Ira", "Tara", "Nisha", "Kavya", "Aditi", "Shreya",
]
LAST_NAMES = [
    "Sharma", "Verma", "Gupta", "Reddy", "Nair", "Iyer", "Patel",
    "Singh", "Kumar", "Das", "Bose", "Chopra", "Kapoor", "Mehta",
    "Joshi", "Pillai", "Rao", "Menon", "Bhatt", "Chauhan", "Yadav",
    "Mishra", "Agarwal", "Kulkarni", "Desai", "Trivedi", "Shah",
]
MONTHS = ["January", "February", "March", "April", "May", "June",
         "July", "August", "September", "October", "November", "December"]

# ═══════════════════════════════════════════════
#  LOGGING / FILES
# ═══════════════════════════════════════════════
os.makedirs(ERRORS_DIR, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(),
              logging.FileHandler(LOG_FILE, encoding="utf-8")],
)
log = logging.getLogger("insta-bot")

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

def proxy_to_url(p):
    """Convert 'user:pass@host:port' or 'host:port' into a URL for requests."""
    if p.startswith("http://") or p.startswith("socks5://"):
        return p
    if "@" in p:
        return f"http://{p}"
    return f"http://{p}"

def read_lines(path):
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        return [l.strip() for l in f if l.strip() and not l.startswith("#")]


def append_line(path, line):
    with open(path, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def mark_email_used(email):
    if email not in set(read_lines(USED_EMAILS_FILE)):
        append_line(USED_EMAILS_FILE, email)


def save_account(username, password, email, full_name, dob):
    is_new = not os.path.exists(ACCOUNTS_FILE)
    with open(ACCOUNTS_FILE, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if is_new:
            w.writerow(["created_at", "username", "password", "email", "full_name", "dob"])
        w.writerow([datetime.now().strftime("%Y-%m-%d %H:%M"),
                    username, password, email, full_name, dob])


def generate_identity():
    first, last = random.choice(FIRST_NAMES), random.choice(LAST_NAMES)
    username = f"{first}.{last}".lower() + str(random.randint(1000, 99999))
    year = random.randint(1987, 2004)
    month = random.choice(MONTHS)
    day = random.randint(1, 28)
    password = (first.capitalize() + "@"
                + ''.join(random.choices("0123456789", k=3))
                + ''.join(random.choices("abcdefghjkmnpqrstuvwxyz", k=4)))
    return {
        "full_name": f"{first} {last}",
        "username": username,
        "password": password,
        "day": str(day), "month": month, "year": str(year),
        "dob": f"{day:02d}/{MONTHS.index(month)+1:02d}/{year}",
    }

def get_proxy():
    """Random proxy from proxies.txt, with auto sticky-session rotation per signup."""
    proxies = read_lines(PROXIES_FILE)
    print(proxies)
    if not proxies:
        return None
    p = random.choice(proxies)
    if "dataimpulse" in p and "__sid" not in p:
        sid = ''.join(random.choices("abcdefghijklmnopqrstuvwxyz0123456789", k=8))
        p = p.replace("__cr.", f"__sid.{sid}__cr.")
    return p



def format_username_colon_pass(p):
    """Allow 'user:pass@host:port' OR 'host:port' formats."""
    return p

# ═══════════════════════════════════════════════
#  BROWSER HELPERS (same logic we got working)
# ═══════════════════════════════════════════════
def find_input_by_label(page, label_text, timeout=5):
    for _ in range(timeout):
        for lb in page.eles('tag:label'):
            try:
                if label_text.lower() in (lb.text or '').strip().lower():
                    for_id = lb.attr('for')
                    if for_id:
                        inp = page.ele(f'@id={for_id}', timeout=2)
                        if inp:
                            return inp
            except Exception:
                pass
        time.sleep(1)
    return None


def fill_custom_dropdown(page, aria_label, option_text):
    combo = page.ele(f'@@role=combobox@@aria-label={aria_label}', timeout=8)
    combo.click()
    time.sleep(random.uniform(0.4, 0.9))
    for opt in page.eles('@@role=option'):
        if (opt.text or '').strip() == option_text:
            opt.click()
            time.sleep(random.uniform(0.2, 0.5))
            return True
    opt = page.ele(f'text:{option_text}', timeout=2)
    if opt:
        opt.click()
        time.sleep(0.4)
        return True
    return False


def human_type(element, text):
    element.clear()
    time.sleep(random.uniform(*MAX_DELAY_JITTER))
    element.input(text)
    time.sleep(random.uniform(*MAX_DELAY_JITTER))


def dismiss_popups(page, max_rounds=8):
    """Click through ALL post-OTP popups (Save login info, phone number, notifications...)."""
    for _ in range(max_rounds):
        clicked = False
        # (text to match, must be exact)
        targets = ["Save", "Save Info", "Not now", "I'll do it later",
                   "Skip", "OK", "Turn on notifications"]
        for btn in page.eles('@@role=button'):
            try:
                t = (btn.text or '').strip()
                if t in targets:
                    btn.click()
                    clicked = True
                    time.sleep(2)
                    break
            except Exception:
                pass
        if not clicked:
            # also try plain buttons
            for btn in page.eles('tag:button'):
                try:
                    t = (btn.text or '').strip()
                    if t in targets:
                        btn.click()
                        clicked = True
                        time.sleep(2)
                        break
                except Exception:
                    pass
        if not clicked:
            break  # no more popups



def get_otp_field(page, timeout=120):
    """Wait for Instagram's OTP input to appear."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        for lbl in ["Enter code", "Verification code", "code",
                    "Confirm code", "Enter the code"]:
            f = (page.ele(f'@@aria-label={lbl}', timeout=1)
                 or find_input_by_label(page, lbl, timeout=1))
            if f:
                return f
        time.sleep(2)
    return None


# ═══════════════════════════════════════════════
#  SESSION STATE (one signup at a time)
# ═══════════════════════════════════════════════
STATE_IDLE, STATE_AWAITING_EMAIL, STATE_SIGNING_UP, STATE_AWAITING_OTP = range(4)

session = {
    "state": STATE_IDLE,
    "chat_id": None,
    "email": None,
    "identity": None,
    "page": None,
    "profile_dir": None,
    "ext_dir": None,
    "otp_deadline": 0,
}
lock = threading.Lock()


def set_state(new_state):
    with lock:
        session["state"] = new_state


def get_state():
    with lock:
        return session["state"]


# ═══════════════════════════════════════════════
#  THE BOT
# ═══════════════════════════════════════════════
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")

WELCOME = (
    "🤖 <b>Instagram Account Creator</b>\n\n"
    "Commands:\n"
    "▶️ /new — start creating an account\n"
    "📋 /accounts — list saved accounts\n"
    "❌ /cancel — abort current signup\n\n"
    "Flow: send email → bot signs up → you send OTP → done ✅"
)


def allowed(msg):
    return not ALLOWED_CHAT_IDS or msg.chat.id in ALLOWED_CHAT_IDS

def make_proxy_auth_extension(proxy_url):
    """DrissionPage/Chrome can't use user:pass proxies directly.
    Build a tiny throwaway extension FOLDER (DrissionPage requires
    an unzipped folder, not a .crx) that handles the proxy auth."""
    from urllib.parse import urlparse

    p = urlparse(proxy_url)
    if not p.username:          # no credentials → plain proxy, no ext needed
        return None

    manifest = {
        "version": "1.0.0",
        "manifest_version": 2,
        "name": "Proxy Auth",
        "permissions": ["proxy", "tabs", "unlimitedStorage", "storage",
                       "<all_urls>", "webRequest", "webRequestBlocking"],
        "background": {"scripts": ["background.js"]},
    }
    background_js = f"""
var config = {{
    mode: "fixed_servers",
    rules: {{
        singleProxy: {{
            scheme: "{p.scheme or 'http'}",
            host: "{p.hostname}",
            port: {p.port}
        }},
        bypassList: ["localhost"]
    }}
}};
chrome.proxy.settings.set({{value: config, scope: "regular"}}, function() {{}});
chrome.webRequest.onAuthRequired.addListener(
    function(details) {{
        return {{
            authCredentials: {{
                username: "{p.username}",
                password: "{p.password}"
            }}
        }};
    }},
    {{urls: ["<all_urls>"]}},
    ['blocking']
);
"""
    ext_dir = tempfile.mkdtemp(prefix="proxy_ext_")
    with open(os.path.join(ext_dir, "manifest.json"), "w") as f:
        f.write(json.dumps(manifest, indent=2))
    with open(os.path.join(ext_dir, "background.js"), "w") as f:
        f.write(background_js)

    return ext_dir          # ← return the FOLDER itself



def launch_fresh_browser(proxy=None):
    """Launch a completely isolated browser. Optionally through an
    authenticated proxy (handled by a generated extension)."""
    opts = ChromiumOptions()
    opts.auto_port()
    opts.set_argument('--start-maximized')
    opts.set_argument('--no-first-run')
    opts.set_argument('--no-default-browser-check')
    opts.set_argument('--no-sandbox')
    # NOTE: removed --incognito! Extensions don't work properly in
    # incognito — and the throwaway user-data-dir gives us isolation anyway.

    if HEADLESS:
        opts.headless(True)
        opts.set_argument('--window-size=1920,1080')

    if proxy:
        crx = make_proxy_auth_extension(proxy)
        if crx:
            opts.add_extension(crx)
            session["ext_dir"] = crx        # ← ADD this
            log.info("🌐 Browser using authenticated proxy via extension")
        else:
            opts.set_proxy(proxy)

    profile_dir = tempfile.mkdtemp(prefix="ig_profile_")
    opts.set_argument(f'--user-data-dir={profile_dir}')
    page = ChromiumPage(addr_or_opts=opts)
    return page, profile_dir


def cleanup_browser(delete_profile=True):
    """Close browser and wipe its temp profile."""
    try:
        if session["page"]:
            session["page"].quit()
    except Exception:
        pass
    session["page"] = None
    # delete temp profile folder so no cookies survive
    if delete_profile and session.get("profile_dir"):
        try:
            shutil.rmtree(session["profile_dir"], ignore_errors=True)
        except Exception:
            pass
    session["profile_dir"] = None
    if session.get("ext_dir"):
        try:
            shutil.rmtree(session["ext_dir"], ignore_errors=True)
        except Exception:
            pass
    session["ext_dir"] = None




def reset_session():
    cleanup_browser(delete_profile=True)
    session["email"] = None
    session["identity"] = None
    set_state(STATE_IDLE)


@bot.message_handler(commands=['start', 'help'])
def cmd_start(msg):
    if not allowed(msg):
        return
    bot.reply_to(msg, WELCOME)


@bot.message_handler(commands=['new'])
def cmd_new(msg):
    if not allowed(msg):
        return
    if get_state() != STATE_IDLE:
        bot.reply_to(msg, "⚠️ A signup is already running. Use /cancel first.")
        return
    session["chat_id"] = msg.chat.id
    set_state(STATE_AWAITING_EMAIL)
    bot.reply_to(msg,
                "📧 Send me the <b>email address</b> to sign up with.\n\n"
                "⚠️ Make sure you can access its inbox — Instagram will "
                "email a verification code.\n\n(or /cancel to abort)")


@bot.message_handler(commands=['cancel'])
def cmd_cancel(msg):
    if not allowed(msg):
        return
    if get_state() == STATE_IDLE:
        bot.reply_to(msg, "Nothing to cancel 🙂")
        return
    email = session["email"]
    # If we already started signup, mark email used so it's never reused
    if email and get_state() == STATE_AWAITING_OTP:
        mark_email_used(email)
    reset_session()
    bot.reply_to(msg, "❌ Signup cancelled. Send /new to start again.")


@bot.message_handler(commands=['accounts'])
def cmd_accounts(msg):
    if not allowed(msg):
        return
    if not os.path.exists(ACCOUNTS_FILE):
        bot.reply_to(msg, "📋 No accounts saved yet. Create one with /new")
        return
    rows = []
    with open(ACCOUNTS_FILE, encoding="utf-8") as f:
        r = list(csv.reader(f))
    if len(r) <= 1:
        bot.reply_to(msg, "📋 No accounts saved yet.")
        return
    lines = [f"📋 <b>Saved accounts ({len(r)-1})</b>\n"]
    for row in r[1:]:
        created, user, pw, email, name, dob = row[:6]
        lines.append(f"👤 <b>@{user}</b>\n"
                     f"🔓 Password: <code>{pw}</code>\n"
                     f"📧 {email}\n"
                     f"🎂 {dob}  |  🕐 {created}\n")
    text = "\n".join(lines)
    # Telegram message limit — send as list + also the raw file
    if len(text) > 4000:
        with open(ACCOUNTS_FILE, "rb") as f:
            bot.send_document(msg.chat.id, f, caption="📋 All saved accounts (CSV)")
    else:
        bot.send_message(msg.chat.id, text)


# ═══════════════════════════════════════════════
#  TEXT HANDLER — email or OTP depending on state
# ═══════════════════════════════════════════════
@bot.message_handler(func=lambda m: True)
def handle_text(msg):
    if not allowed(msg):
        return
    text = msg.text.strip()
    chat_id = msg.chat.id

    # ── Expecting an EMAIL ──
    if session.get("chat_id") == chat_id and get_state() == STATE_AWAITING_EMAIL:
        email = text.lower()
        if not EMAIL_RE.match(email):
            bot.reply_to(msg, "❌ That doesn't look like a valid email. Try again:")
            return
        if email in set(read_lines(USED_EMAILS_FILE)):
            bot.reply_to(msg, "🔁 That email was already used! Send a different one:")
            return
        session["chat_id"] = chat_id
        session["email"] = email
        bot.send_message(chat_id, "⏳ Starting signup — takes ~30 seconds...")
        threading.Thread(target=do_signup, args=(chat_id, email), daemon=True).start()

    # ── Expecting the OTP ──
    elif session.get("chat_id") == chat_id and get_state() == STATE_AWAITING_OTP:
        if not text.isdigit() or len(text) not in (4, 5, 6, 8):
            bot.reply_to(msg, "❌ OTP should be digits only. Send just the code:")
            return
        if time.time() > session.get("otp_deadline", 0):
            mark_email_used(session["email"])
            reset_session()
            bot.reply_to(msg, "⌛ OTP timed out. Send /new to start again.")
            return
        bot.send_message(chat_id, "⏳ Verifying code...")
        threading.Thread(target=do_submit_otp, args=(chat_id, text), daemon=True).start()

    else:
        bot.reply_to(msg, "Send /new to start creating an account 🙂")

@bot.message_handler(commands=['screenshot'])
def cmd_screenshot(msg):
    if not allowed(msg):
        return
    if not session.get("page"):
        bot.reply_to(msg, "No browser running right now 🙂")
        return
    try:
        path = os.path.join(ERRORS_DIR, "live_screenshot.png")
        session["page"].get_screenshot(path)
        with open(path, "rb") as f:
            bot.send_photo(msg.chat.id, f, caption="📸 Live view of the bot's browser")
    except Exception as e:
        bot.reply_to(msg, f"❌ Couldn't screenshot: {e}")


# ═══════════════════════════════════════════════
#  SIGNUP WORKER (runs in background thread)
# ═══════════════════════════════════════════════
# ═══════════════════════════════════════════════
#  SIGNUP WORKER (runs in background thread)
# ═══════════════════════════════════════════════
def do_signup(chat_id, email):
    set_state(STATE_SIGNING_UP)
    identity = generate_identity()
    session["identity"] = identity

    bot.send_message(chat_id,
        f"👤 Generated identity:\n"
        f"   Name: <b>{identity['full_name']}</b>\n"
        f"   Username: <b>@{identity['username']}</b>\n"
        f"   Password: <code>{identity['password']}</code>\n"
        f"   DOB: {identity['dob']}\n"
        f"🌐 Opening browser...")

    proxy = get_proxy()
    if proxy:
        bot.send_message(chat_id, f"🌐 Using proxy: <code>{proxy}</code>")

    # ── Verify proxy IP before signup ──        ← ══ PUT IT HERE ══
    exit_ip = "direct (no proxy)"
    if proxy:
        try:
            import requests
            r = requests.get("https://api.ipify.org",
                             proxies={"http": proxy_to_url(proxy),
                                      "https": proxy_to_url(proxy)},
                             timeout=10)
            exit_ip = r.text
        except Exception as e:
            bot.send_message(chat_id,
                             f"❌ Proxy not responding: {e}\n"
                             f"Trying direct connection instead...")
            proxy = None
    bot.send_message(chat_id, f"🌐 Exit IP: <code>{exit_ip}</code>")
    # ─────────────────────────────────
    page, profile_dir = launch_fresh_browser(proxy=proxy)
    session["page"] = page
    session["profile_dir"] = profile_dir

    try:
        page.get('https://www.instagram.com/accounts/emailsignup/')
        time.sleep(random.uniform(4, 7))

        if 'challenge' in page.url or 'login' in page.url:
            bot.send_message(chat_id, "🚫 Looks like rate-limited/blocked. Aborting.")
            reset_session()  # email NOT marked used — can retry
            return

        # Wait for form
        email_box = None
        for _ in range(15):
            email_box = find_input_by_label(page, "Mobile number or email", timeout=1)
            if email_box:
                break
            time.sleep(1)
        if not email_box:
            raise Exception("Signup form did not load")

        bot.send_message(chat_id, "✍️ Filling signup form...")
        human_type(email_box, email)

        pass_box = page.ele('@type=password', timeout=8)
        if not pass_box:
            raise Exception("Password field not found")
        human_type(pass_box, identity["password"])

        ok = (fill_custom_dropdown(page, "Select day", identity["day"]) and
              fill_custom_dropdown(page, "Select month", identity["month"]) and
              fill_custom_dropdown(page, "Select year", identity["year"]))
        if not ok:
            raise Exception("Could not set date of birth")
        time.sleep(random.uniform(0.5, 1.2))

        name_box = find_input_by_label(page, "Full name")
        if name_box:
            human_type(name_box, identity["full_name"])

        user_box = page.ele('@type=search', timeout=8) or find_input_by_label(page, "Username")
        if not user_box:
            raise Exception("Username field not found")

        def username_is_taken(page):
            """Check all variants of the username-error text."""
            for t in ["is not available", "isn't available",
                      "already been taken", "is taken",
                      "Please try another username"]:
                if page.ele(f'text:{t}', timeout=1):
                    return True
            return False

        def make_new_username():
            """Generate a fresh, more unique username."""
            first = identity["full_name"].split(" ")[0].lower()
            last = identity["full_name"].split(" ")[-1].lower()
            new_u = f"{first}.{last}" + str(random.randint(1000, 99999))
            # Instagram max username = 30 chars
            if len(new_u) > 28:
                new_u = (first + last)[:15] + str(random.randint(10000, 999999))
            return new_u

        # ── Username fill + submit loop with retries ──
        MAX_USERNAME_ATTEMPTS = 5
        for attempt in range(1, MAX_USERNAME_ATTEMPTS + 1):
            human_type(user_box, identity["username"])

            # Let Instagram validate the username (it checks as you type)
            time.sleep(3)

            if username_is_taken(page):
                old = identity["username"]
                new_u = make_new_username()
                bot.send_message(chat_id,
                    f"⚠️ @{old} taken — trying @{new_u} "
                    f"(attempt {attempt}/{MAX_USERNAME_ATTEMPTS})")
                identity["username"] = new_u
                continue   # re-type new username and re-check

            # ── Username looks OK — Submit (div role=button) ──
            submitted = False
            for btn in page.eles('@@role=button'):
                if (btn.text or '').strip() == 'Submit':
                    btn.click()
                    submitted = True
                    break
            if not submitted:
                raise Exception("Submit button not found")

            time.sleep(4)

            # ── After submit: did Instagram reject the username anyway? ──
            if username_is_taken(page):
                old = identity["username"]
                new_u = make_new_username()
                bot.send_message(chat_id,
                    f"⚠️ @{old} rejected after submit — trying @{new_u} "
                    f"(attempt {attempt}/{MAX_USERNAME_ATTEMPTS})")
                identity["username"] = new_u
                continue   # loop back, re-type and resubmit

            # No username error → we're past the form! 🎉
            break

        else:
            # All attempts failed
            raise Exception("Couldn't find an available username after "
                            f"{MAX_USERNAME_ATTEMPTS} tries")

        bot.send_message(chat_id, "📨 Submitted! Waiting for Instagram to ask for the code...")

        # Wait for OTP input to appear
        otp_field = get_otp_field(page, timeout=120)
        if not otp_field:
            try:
                page.get_screenshot(os.path.join(ERRORS_DIR, f"no_otp_{identity['username']}.png"))
            except Exception:
                pass
            bot.send_message(chat_id,
                "❌ No OTP screen appeared (may need captcha or number). "
                "Check the browser on the server. Send /cancel or type the code "
                "if you solved something manually.")
            # Give the user a chance to handle manually — still accept OTP input
            session["otp_deadline"] = time.time() + OTP_TIMEOUT
            set_state(STATE_AWAITING_OTP)
            return

        session["otp_deadline"] = time.time() + OTP_TIMEOUT
        set_state(STATE_AWAITING_OTP)
        bot.send_message(chat_id,
            f"🔢 Instagram sent a code to <b>{email}</b>\n\n"
            f"💬 Send me the OTP code now (digits only).\n"
            f"⌛ You have {OTP_TIMEOUT // 60} minutes.")

    except Exception as e:
        log.exception("Signup failed")
        try:
            page.get_screenshot(os.path.join(ERRORS_DIR, f"error_{identity['username']}.png"))
        except Exception:
            pass
        bot.send_message(chat_id, f"❌ Signup failed: {e}\n"
                                  f"Email not used up — you can retry it.")
        reset_session()



# ═══════════════════════════════════════════════
#  OTP SUBMIT WORKER
# ═══════════════════════════════════════════════
def do_submit_otp(chat_id, code):
    page = session["page"]
    identity = session["identity"]
    email = session["email"]
    set_state(STATE_SIGNING_UP)
    try:
        otp_field = get_otp_field(page, timeout=15)
        if not otp_field:
            raise Exception("OTP input disappeared")

        human_type(otp_field, code)
        bot.send_message(chat_id, "⏳ Verifying... (this can take ~30s)")

        # ── THE FIX: click the Continue/Confirm button after entering code ──
        # Instagram's email-code page uses a button labeled "Continue"
        # (sometimes "Confirm" depending on page version).
        time.sleep(2)

        # Method 1: exact-text match on all button-like elements
        clicked = False
        for t in ["Continue", "Confirm", "Next", "Verify", "Submit", "OK"]:
            # check divs with role=button AND real <button> tags
            for btn in page.eles('@@role=button') + page.eles('tag:button'):
                try:
                    if (btn.text or '').strip().lower() == t.lower():
                        btn.click()
                        clicked = True
                        bot.send_message(chat_id, f"👉 Clicked '{t}' button")
                        break
                except Exception:
                    pass
            if clicked:
                break

        # Method 2: if no button found, press Enter in the OTP field
        if not clicked:
            try:
                otp_field.input('\n')   # sends Enter keypress
                bot.send_message(chat_id, "↩️ Pressed Enter in code field")
            except Exception:
                pass

        # Method 3: last resort — any submit-type button on the page
        if not clicked:
            try:
                for btn in page.eles('@type=submit'):
                    try:
                        btn.click()
                        clicked = True
                        break
                    except Exception:
                        pass
            except Exception:
                pass

        time.sleep(5)

        # ── Wait patiently for verification + popup chain ──
        success = False
        for i in range(30):  # up to ~90 seconds
            time.sleep(3)
            url = page.url

            # Click through any popup dialogs (Save login info etc.)
            dismiss_popups(page)

            # Keep clicking Continue if a second one appears
            # (Instagram sometimes has a 2-step code confirmation)
            if i < 5:  # only during the first 15 seconds
                for btn in page.eles('@@role=button') + page.eles('tag:button'):
                    try:
                        if (btn.text or '').strip() == "Continue":
                            btn.click()
                            time.sleep(2)
                            break
                    except Exception:
                        pass

            # Captcha / challenge screen?
            if 'challenge' in url:
                bot.send_message(chat_id,
                    "🤖 Instagram is showing a security check!\n"
                    "Solve it in the browser within 60 seconds...")
                chall_deadline = time.time() + 90
                while 'challenge' in page.url and time.time() < chall_deadline:
                    time.sleep(3)
                    dismiss_popups(page)

            # ── Success checks (multiple signals) ──
            url = page.url
            logged_in_markers = [
                page.ele('text:Search', timeout=1),
                page.ele('text:Home', timeout=1),
                page.ele('text:Notifications', timeout=1),
                page.ele('text:For You', timeout=1),
            ]
            on_signup = ('emailsignup' in url or 'challenge' in url
                         or 'login' in url or page.ele('text:Date of birth', timeout=1))

            if any(logged_in_markers) and not on_signup:
                success = True
                break
            if 'instagram.com/' == url.rstrip('/') + '/' and not on_signup:
                success = True
                break

        if success:
            dismiss_popups(page)
            # Grab credentials BEFORE wiping the browser
            saved_username = identity["username"]
            saved_password = identity["password"]
            saved_name = identity["full_name"]
            saved_dob = identity["dob"]
            mark_email_used(email)
            save_account(saved_username, saved_password, email, saved_name, saved_dob)

            # ── Full clean reset FIRST, then message ──
            cleanup_browser(delete_profile=True)  # closes browser + wipes profile
            session["email"] = None
            session["identity"] = None
            set_state(STATE_AWAITING_EMAIL)  # ← ready for next email

            bot.send_message(chat_id,
                             f"🎉 <b>Account created!</b>\n\n"
                             f"👤 Username: <b>@{saved_username}</b>\n"
                             f"🔓 Password: <code>{saved_password}</code>\n"
                             f"📧 {email}\n"
                             f"💾 Saved to accounts.csv\n\n"
                             f"➡️ <b>Send the next email now</b> to create another account.")

        else:
            try:
                page.get_screenshot(os.path.join(ERRORS_DIR,
                                      f"bad_otp_{identity['username']}.png"))
            except Exception:
                pass
            bot.send_message(chat_id,
                "❓ Couldn't confirm success.\n"
                "Look at the browser window:\n"
                "• Popups showing → click them yourself\n"
                "• Wrong code message → send correct code again\n"
                "• /cancel to abort")
            session["otp_deadline"] = time.time() + OTP_TIMEOUT
            set_state(STATE_AWAITING_OTP)

    except Exception as e:
        log.exception("OTP submit failed")
        bot.send_message(chat_id, f"❌ Error submitting OTP: {e}")
        mark_email_used(email)
        reset_session()



# ═══════════════════════════════════════════════
#  RUN
# ═══════════════════════════════════════════════
if __name__ == "__main__":
    if BOT_TOKEN.startswith("PASTE"):
        print("❌ Set your BOT_TOKEN at the top of the file first!")
        raise SystemExit(1)

    # Verify token works before starting
    import requests
    try:
        r = requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/getMe", timeout=10)
        info = r.json()
        if info.get("ok"):
            print(f"✅ Token OK — bot is @{info['result']['username']}")
        else:
            print("❌ Token invalid! Response:", info)
            raise SystemExit(1)
    except Exception as e:
        print(f"❌ Cannot reach Telegram API: {e}")
        print("   Check internet / token.")
        raise SystemExit(1)

    log.info("Bot starting — polling for messages...")
    print("🤖 Bot is running. Press Ctrl+C to stop.")
    bot.infinity_polling(timeout=60)

