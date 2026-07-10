"""
QA signup automation for cricmatch247.com

Drives the "New Member? Register Now" modal end-to-end so you can smoke-test
your own registration flow after changes. Submits and reports the outcome with
a screenshot for each attempt.

This is a QA test driver, not a mass-registration bot.

Default flow:
    python main.py
  -> generates a random name, username, email, and a policy-compliant password
  -> asks you for the phone number
  -> fills and submits the signup form
  -> when the site shows the "enter 6-digit OTP" screen, asks you for the OTP
     (sent by SMS to that phone) and fills + verifies it
  -> reports the result

Every generated credential set is printed and saved to accounts.db (SQLite) so
you can retrieve it later for repeat testing:
    python main.py --list            # show recent stored accounts
    python main.py --list --limit 50

Note: randomly generated emails are not real inboxes, so email verification
can't be completed for them. To test the full verify-and-activate path, pass a
real address with --email.

Other options:
    python main.py --headed           # watch it in a real browser window
    python main.py --no-submit        # fill but don't click REGISTER
    python main.py --phone 9876543210 # skip the prompt, pass phone directly
    python main.py --email you@gmail.com   # override the random email
    python main.py --account-file accounts.json  # batch from a JSON file
    python main.py --proxy host:port:username:password  # route through a proxy
    python main.py --proxy http://username:password@host:port
"""
import argparse
import json
import random
import socket
import string
import subprocess
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright, Error as PWError, TimeoutError as PWTimeout

import db

SITE_URL = "https://cricmatch247.com?btag=211079"

FIRST_NAMES = ["aarav", "vivaan", "aditya", "vihaan", "arjun", "sai", "reyansh",
               "ayaan", "krishna", "ishaan", "rohan", "kabir", "dhruv", "karan",
               "rahul", "amit", "vikram", "arnav", "dev", "aryan",
               "ananya", "diya", "saanvi", "aadhya", "kiara", "myra", "pari",
               "anika", "ishita", "riya", "priya", "neha", "pooja", "sneha",
               "kavya", "meera", "shreya", "tanvi", "isha", "aarohi"]
LAST_NAMES = ["sharma", "verma", "gupta", "singh", "kumar", "patel", "reddy",
              "nair", "iyer", "rao", "mehta", "joshi", "agarwal", "bhatt",
              "choudhary", "malhotra", "kapoor", "chatterjee", "mukherjee",
              "banerjee", "das", "dutta", "pillai", "menon", "naidu",
              "shetty", "hegde", "bose", "sinha", "tiwari"]
EMAIL_DOMAIN = "gmail.com"

# Selectors captured from the live signup modal.
SEL = {
    "open_modal": [".registerUserData", "button.headerjoinBtn", "button.cls_reg_btn", ".join__btn"],
    "close_popup": [".mnPopupClose", ".pgSoftClsBtn", ".support_popup_close",
                    ".areSurecancelBtn", "button:has-text('Close')"],
    "username": "#userNameid",
    "email": "#userEmailid",
    "password": "#pass_log_id",
    "phone": "#phoneNumber",
    "terms": "#remChck2",
    "submit": "button.cls_register_new",
    # Inline "The mobile number has already been taken." error (a bare <li>
    # inside this <ul> -- NOT caught by the generic toast/.error_msg scraper).
    "phone_taken_error": ".err_phone",
    # Signup OTP screen (distinct from the "Login with OTP" widget).
    "otp_popup": ".signup_otp_popup, .otpRegisterForm",
    "otp_digits": "input.otp__digit_signup",
    # Ordered candidates for the signup "VERIFY" button; click the first VISIBLE
    # one (the page also has a hidden login-OTP verify button).
    "otp_verify": ["a.get_user_otp", ".vf_otpBtn a", ".vf_num_otpSec a.mb-button",
                   ".signup_otp_popup a:has-text('Verify')"],
    "otp_error": ".otp_error",
}

SHOTS_DIR = Path("shots")


def gen_password():
    """Build a password that satisfies the form policy:
    5-60 chars, >=1 digit, >=1 special, upper and lower case."""
    pools = [random.choice(string.ascii_uppercase),
             random.choice(string.ascii_lowercase),
             random.choice(string.digits),
             random.choice("!@#$%^&*")]
    pools += random.choices(string.ascii_letters + string.digits, k=6)
    random.shuffle(pools)
    return "".join(pools)


def gen_account():
    """Generate a random test identity. Phone is filled in separately."""
    first = random.choice(FIRST_NAMES)
    last = random.choice(LAST_NAMES)
    tag = random.randint(100, 9999)
    username = f"{first}{last}{tag}"
    email = f"{first}.{last}{tag}@{EMAIL_DOMAIN}"
    return {"username": username, "email": email, "password": gen_password()}


def parse_proxy(proxy_str):
    """Parse a proxy string into a Playwright proxy dict ({"server", "username",
    "password"}). Accepts, with an optional leading "scheme://" (http, https,
    or socks5 -- defaults to http if omitted):
        [scheme://]host:port
        [scheme://]host:port:username:password
        scheme://username:password@host:port
    Examples: "proxy.host:1234", "proxy.host:1234:user:pass",
    "socks5://proxy.host:1080:user:pass", "http://user:pass@proxy.host:1234".
    Returns None for an empty/falsy input; raises ValueError for anything else
    unrecognized."""
    if not proxy_str:
        return None

    scheme = "http"
    rest = proxy_str
    if "://" in proxy_str:
        scheme, rest = proxy_str.split("://", 1)

    if "@" in rest:
        userinfo, hostport = rest.rsplit("@", 1)
        username, _, password = userinfo.partition(":")
        host, _, port = hostport.partition(":")
        if not host or not port:
            raise ValueError(f"Proxy URL missing host or port: {proxy_str}")
        proxy = {"server": f"{scheme}://{host}:{port}"}
        if username:
            proxy["username"] = username
        if password:
            proxy["password"] = password
        return proxy

    parts = rest.split(":")
    if len(parts) == 2:
        host, port = parts
        return {"server": f"{scheme}://{host}:{port}"}
    if len(parts) == 4:
        host, port, username, password = parts
        return {"server": f"{scheme}://{host}:{port}", "username": username, "password": password}
    raise ValueError(
        f"Unrecognized proxy format: {proxy_str!r} (expected host:port, "
        "host:port:username:password, optionally prefixed scheme://, e.g. socks5://)"
    )


def _free_local_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def maybe_bridge_proxy(proxy_conf, timeout=5):
    """Chromium cannot authenticate to a SOCKS5 proxy itself (a real Chromium
    limitation, confirmed via a live error: "Browser does not support socks5
    proxy authentication") -- only HTTP(S) proxies support username/password
    at the browser level. If `proxy_conf` is SOCKS5 with credentials, spin up
    a local `pproxy` process that does the SOCKS5 auth itself and exposes an
    unauthenticated local HTTP proxy for Chromium to use instead.

    pproxy expects upstream SOCKS5 credentials in the URL *fragment*, not the
    userinfo position (which it reserves for shadowsocks cipher specs):
    "socks5://host:port#username:password". Verified live against a real
    ProxyCheap SOCKS5 proxy.

    Returns (proxy_conf_to_actually_use, bridge_process_or_None). Caller must
    pass the returned process to stop_bridge() when done, even on failure
    paths -- pproxy is left running otherwise."""
    if not proxy_conf or not proxy_conf.get("server", "").startswith("socks5://") \
            or not proxy_conf.get("username"):
        return proxy_conf, None

    host_port = proxy_conf["server"].split("://", 1)[1]
    upstream = f"socks5://{host_port}#{proxy_conf['username']}:{proxy_conf.get('password', '')}"
    local_port = _free_local_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "pproxy", "-l", f"http://127.0.0.1:{local_port}", "-r", upstream],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            proc.wait()
            raise RuntimeError(
                "Local proxy bridge (pproxy) exited immediately -- is it installed? "
                "pip install pproxy"
            )
        try:
            with socket.create_connection(("127.0.0.1", local_port), timeout=0.3):
                break
        except OSError:
            time.sleep(0.15)
    else:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
        raise RuntimeError("Local proxy bridge (pproxy) did not start listening in time.")

    return {"server": f"http://127.0.0.1:{local_port}"}, proc


def stop_bridge(proc):
    if not proc:
        return
    proc.terminate()
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()


def dismiss_popups(page):
    """Close promo / support overlays that block the header buttons."""
    for sel in SEL["close_popup"]:
        try:
            loc = page.locator(sel)
            for i in range(loc.count()):
                item = loc.nth(i)
                if item.is_visible():
                    item.click(timeout=1500)
        except Exception:
            pass
    page.wait_for_timeout(800)


def open_signup_modal(page):
    """Click JOIN and wait for the register form to appear."""
    dismiss_popups(page)
    for sel in SEL["open_modal"]:
        try:
            page.locator(sel).first.click(timeout=4000, force=True)
            page.wait_for_selector(SEL["username"], state="visible", timeout=8000)
            return True
        except Exception:
            continue
    return False


def read_result(page):
    """Best-effort read of any toast / validation message shown after submit."""
    messages = []
    for sel in [".toast", ".toast-message", ".swal2-title", ".swal2-html-container",
                ".error_msg", ".invalid_msg", "[class*=toast]", "[class*=alert]"]:
        try:
            loc = page.locator(sel)
            for i in range(min(loc.count(), 5)):
                item = loc.nth(i)
                if item.is_visible():
                    txt = (item.inner_text() or "").strip()
                    if txt:
                        messages.append(txt)
        except Exception:
            pass
    # dedupe, keep order
    seen, out = set(), []
    for m in messages:
        if m not in seen:
            seen.add(m)
            out.append(m)
    return out


def check_phone_taken(page):
    """Return the 'mobile number already taken' error text if it's showing,
    else None. Not a toast -- a bare <li> inside .err_phone."""
    try:
        el = page.locator(SEL["phone_taken_error"]).first
        if el.count() and el.is_visible():
            txt = (el.inner_text() or "").strip()
            if txt:
                return txt
    except Exception:
        pass
    return None


def wait_for_register_outcome(page, timeout_ms=12000, poll_ms=250):
    """After clicking REGISTER, poll for whichever outcome shows up first
    instead of blindly sleeping: the OTP screen, the phone-taken error, or any
    other toast/inline error. Measured live: phone-taken typically renders in
    under 0.5s, so polling beats a flat sleep on the common paths without
    lowering the ceiling for slow ones."""
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        if check_phone_taken(page):
            return "phone_taken"
        try:
            if page.locator(SEL["otp_digits"]).first.is_visible():
                return "otp"
        except Exception:
            pass
        if read_result(page):
            return "error"
        page.wait_for_timeout(poll_ms)
    return "timeout"


def wait_for_otp_outcome(page, timeout_ms=10000, poll_ms=250):
    """After clicking the OTP Verify button, poll for either the inline OTP
    error appearing or the OTP screen closing (success), instead of a flat
    sleep."""
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        try:
            e = page.locator(SEL["otp_error"]).first
            if e.count() and e.is_visible():
                return "error"
        except Exception:
            pass
        try:
            still_open = (page.locator(SEL["otp_digits"]).first.is_visible()
                         if page.locator(SEL["otp_digits"]).count() else False)
        except Exception:
            still_open = False
        if not still_open:
            return "closed"
        page.wait_for_timeout(poll_ms)
    return "timeout"


def click_first_visible(page, selectors, timeout=6000):
    """Click the first visible+enabled match across the given selectors."""
    for sel in selectors:
        loc = page.locator(sel)
        for i in range(loc.count()):
            item = loc.nth(i)
            try:
                if item.is_visible():
                    item.click(timeout=timeout)
                    return True
            except Exception:
                continue
    return False


def prompt_otp(digits):
    """Ask for the OTP interactively until it's the right number of digits."""
    while True:
        otp = input(f"Enter the {digits}-digit OTP sent by SMS: ").strip()
        if otp.isdigit() and len(otp) == digits:
            return otp
        print(f"  Please enter exactly {digits} digits.")


def enter_otp(page, acct, result):
    """After REGISTER, wait for the signup OTP popup, ask the user for the code,
    fill the digit boxes, and click Verify. Mutates and returns `result`."""
    try:
        page.wait_for_selector(SEL["otp_digits"], state="visible", timeout=15000)
    except PWTimeout:
        result["messages"].append("No OTP screen appeared after REGISTER "
                                   "(check the result screenshot).")
        return result

    boxes = page.locator(SEL["otp_digits"])
    n = boxes.count()
    if n == 0:
        result["messages"].append("OTP screen detected but no digit inputs found.")
        return result

    print(f"\nOTP screen is up — an SMS code was sent to {acct.get('phone', 'your phone')}.")
    otp = prompt_otp(n)

    # Type one digit per box; the widget auto-advances, but set focus explicitly
    # so it works even if the auto-advance handler misbehaves.
    for i, ch in enumerate(otp):
        box = boxes.nth(i)
        box.click()
        box.press_sequentially(ch, delay=40)
    page.wait_for_timeout(500)

    stamp = time.strftime("%Y%m%d-%H%M%S")
    otp_filled = SHOTS_DIR / f"{acct['username']}-{stamp}-otp-filled.png"
    page.screenshot(path=str(otp_filled))

    if not click_first_visible(page, SEL["otp_verify"], timeout=6000):
        result["messages"].append("Could not find a visible Verify button.")
        result["shot"] = str(otp_filled)
        return result
    outcome = wait_for_otp_outcome(page)

    otp_result = SHOTS_DIR / f"{acct['username']}-{stamp}-otp-result.png"
    page.screenshot(path=str(otp_result))
    result["shot"] = str(otp_result)

    # Did the site reject the OTP?
    err = ""
    if outcome == "error":
        try:
            e = page.locator(SEL["otp_error"]).first
            if e.count() and e.is_visible():
                err = (e.inner_text() or "").strip()
        except Exception:
            pass
    still_open = outcome in ("error", "timeout")

    msgs = read_result(page)
    if err:
        result["ok"] = False
        result["messages"].append(f"OTP rejected: {err}")
    elif still_open:
        result["ok"] = False
        result["messages"].append("OTP screen still showing after Verify — "
                                   "likely wrong/expired code.")
    else:
        result["ok"] = True
        result["messages"].append("OTP verified — account appears registered.")
    result["messages"].extend(m for m in msgs if m not in result["messages"])
    return result


def signup_once(page, acct, submit=True, interactive=False, site_url=None):
    """Run one signup attempt. Returns a result dict.

    If the site rejects the phone number as already registered and
    `interactive` is True, prompts for a different phone number and retries
    (up to 5 times) instead of failing outright."""
    result = {"account": acct.get("username", "?"), "ok": None, "messages": [], "shot": None}

    page.goto(site_url or SITE_URL, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(4000)

    if not open_signup_modal(page):
        SHOTS_DIR.mkdir(exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        no_modal_shot = SHOTS_DIR / f"{acct.get('username', 'unknown')}-{stamp}-no-modal.png"
        page.screenshot(path=str(no_modal_shot))
        result["ok"] = False
        result["messages"] = ["Could not open the signup modal (JOIN button)."]
        result["shot"] = str(no_modal_shot)
        return result

    # Type (not fill) so the site's live validation/keyup handlers fire; blur
    # each field afterward to trigger any on-blur checks.
    for sel, value in [(SEL["username"], acct["username"]),
                       (SEL["email"], acct["email"]),
                       (SEL["password"], acct["password"]),
                       (SEL["phone"], str(acct["phone"]))]:
        field = page.locator(sel)
        field.click()
        field.press_sequentially(value, delay=30)
        field.blur()

    # Ensure the "I'm over 18 + T&C" box is checked.
    try:
        cb = page.locator(SEL["terms"])
        if cb.count() and not cb.is_checked():
            cb.check(force=True)
    except Exception:
        pass

    SHOTS_DIR.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    filled_shot = SHOTS_DIR / f"{acct['username']}-{stamp}-filled.png"
    page.screenshot(path=str(filled_shot))

    if not submit:
        result["ok"] = True
        result["messages"] = ["--no-submit: form filled but not submitted."]
        result["shot"] = str(filled_shot)
        return result

    page.click(SEL["submit"])
    outcome = wait_for_register_outcome(page)

    attempts = 0
    while outcome == "phone_taken" and interactive and attempts < 5:
        attempts += 1
        phone_err = check_phone_taken(page)
        print(f"\n{phone_err} Try a different phone number.")
        acct["phone"] = prompt_phone()
        phone_field = page.locator(SEL["phone"])
        phone_field.fill("")
        phone_field.click()
        phone_field.press_sequentially(str(acct["phone"]), delay=30)
        phone_field.blur()
        page.click(SEL["submit"])
        outcome = wait_for_register_outcome(page)

    msgs = read_result(page)
    result_shot = SHOTS_DIR / f"{acct['username']}-{stamp}-result.png"
    page.screenshot(path=str(result_shot))
    result["shot"] = str(result_shot)

    if outcome == "phone_taken":
        phone_err = check_phone_taken(page)
        result["ok"] = False
        result["messages"] = [phone_err or "The mobile number has already been taken."]
        if attempts:
            result["messages"].append(f"Gave up after {attempts} retr{'y' if attempts == 1 else 'ies'}.")
        return result

    if outcome in ("error", "timeout"):
        result["ok"] = False
        result["messages"] = msgs or ["REGISTER did not lead to the OTP screen (check the screenshot)."]
        return result

    result["messages"] = msgs
    # outcome == "otp" -> the site sent an SMS OTP; handle the verify step.
    return enter_otp(page, acct, result)


def prompt_phone():
    """Ask for the phone number interactively."""
    while True:
        phone = input("Enter phone number for this signup: ").strip()
        if phone.isdigit() and 7 <= len(phone) <= 15:
            return phone
        print("  Please enter digits only (7-15 characters).")


def load_accounts(args):
    # Batch mode: everything comes from the file as-is; a per-account "proxy"
    # / "url" key overrides --proxy / --url, which fill in accounts that omit them.
    if args.account_file:
        accts = json.loads(Path(args.account_file).read_text())
        accts = [accts] if isinstance(accts, dict) else accts
        for a in accts:
            a.setdefault("proxy", args.proxy)
            a.setdefault("url", args.url)
        return accts

    # Default: generate a random identity, keep any explicit overrides.
    acct = gen_account()
    if args.username:
        acct["username"] = args.username
    if args.email:
        acct["email"] = args.email
    if args.password:
        acct["password"] = args.password
    acct["phone"] = args.phone or prompt_phone()
    acct["proxy"] = args.proxy
    acct["url"] = args.url

    print("\nGenerated account:")
    print(f"  username : {acct['username']}")
    print(f"  email    : {acct['email']}")
    print(f"  password : {acct['password']}")
    print(f"  phone    : {acct['phone']}")
    print(f"  proxy    : {acct['proxy'] or '(none — direct connection)'}")
    print(f"  url      : {acct['url'] or SITE_URL} {'(default)' if not acct['url'] else ''}\n")
    return [acct]


def main():
    ap = argparse.ArgumentParser(description="QA signup driver for cricmatch247.com")
    ap.add_argument("--username", help="override the random username")
    ap.add_argument("--email", help="override the random email (use a real one to test verification)")
    ap.add_argument("--password", help="override the random password")
    ap.add_argument("--phone", help="phone number (skips the interactive prompt)")
    ap.add_argument("--account-file", help="JSON file with a list of test accounts")
    ap.add_argument("--proxy", help="host:port, host:port:username:password, or a scheme:// URL")
    ap.add_argument("--url", help=f"override the site URL (default: {SITE_URL})")
    ap.add_argument("--headed", action="store_true", help="show the browser window")
    ap.add_argument("--no-submit", action="store_true", help="fill but don't click REGISTER")
    ap.add_argument("--list", action="store_true",
                    help="print stored accounts from accounts.db and exit")
    ap.add_argument("--limit", type=int, default=20, help="rows to show with --list")
    ap.add_argument("--status", help="filter --list/--export-csv to one status, "
                                     "e.g. success, failed, phone_taken")
    ap.add_argument("--export-csv", nargs="?", const="accounts_export.csv", default=None,
                    metavar="PATH", help="export stored accounts to a CSV file and exit "
                                         "(default path: accounts_export.csv; ALL accounts "
                                         "unless --status filters it)")
    args = ap.parse_args()

    conn = db.get_connection()

    if args.list:
        db.print_accounts(conn, limit=args.limit, status=args.status)
        return

    if args.export_csv:
        count = db.export_csv(conn, args.export_csv, status=args.status)
        print(f"Exported {count} account(s) to {args.export_csv}")
        return

    accounts = load_accounts(args)
    results = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.headed)
        for acct in accounts:
            row_id = db.insert_account(conn, acct)
            bridge_proc = None
            try:
                proxy_conf = parse_proxy(acct.get("proxy"))
                proxy_conf, bridge_proc = maybe_bridge_proxy(proxy_conf)
            except (ValueError, RuntimeError) as e:
                print(f"[FAIL] {acct.get('username', '?')}: {e}")
                db.update_status(conn, row_id, "failed", notes=str(e))
                stop_bridge(bridge_proc)
                continue
            context = browser.new_context(proxy=proxy_conf) if proxy_conf else None
            page = context.new_page() if context else browser.new_page()
            try:
                res = signup_once(page, acct, submit=not args.no_submit,
                                  interactive=not args.account_file,
                                  site_url=acct.get("url"))
            except PWTimeout as e:
                res = {"account": acct.get("username", "?"), "ok": False,
                       "messages": [f"Timeout: {str(e)[:120]}"], "shot": None}
            except PWError as e:
                # e.g. a broken/unreachable proxy raises this, not PWTimeout.
                res = {"account": acct.get("username", "?"), "ok": False,
                       "messages": [f"Browser error (check --proxy?): {str(e)[:200]}"], "shot": None}
            finally:
                page.close()
                if context:
                    context.close()
                stop_bridge(bridge_proc)
            results.append(res)
            print(f"[{'OK ' if res['ok'] else 'FAIL' if res['ok'] is False else '?  '}] "
                  f"{res['account']}: {' | '.join(res['messages'])}")
            if res["shot"]:
                print(f"       screenshot: {res['shot']}")

            status = "success" if res["ok"] else ("failed" if res["ok"] is False else "unknown")
            db.update_status(conn, row_id, status,
                              notes="; ".join(res["messages"])[:500], screenshot=res["shot"])
        browser.close()

    ok = sum(1 for r in results if r["ok"])
    print(f"\n{ok}/{len(results)} attempt(s) looked successful.")
    print("Stored in accounts.db — run `python main.py --list` to view.")
    sys.exit(0 if ok == len(results) else 1)


if __name__ == "__main__":
    main()
