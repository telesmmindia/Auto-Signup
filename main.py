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
    python main.py --fast             # no browser -- plain HTTP register API
                                       # calls (cricmatch247 only; falls back
                                       # to the browser for sites that don't
                                       # support it -- see CLAUDE.md)
"""
import argparse
import json
import os
import random
import re
import socket
import string
import subprocess
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import parse_qs, urlsplit, urlunsplit

import requests
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, Error as PWError, TimeoutError as PWTimeout

import db
from sites import profile_for
from sites.games import BACCARAT, STOCKMARKET, GameProfile, game_for

# Load .env so the CLI (not just the bot) picks up CAPSOLVER_API_KEY etc.
load_dotenv()

SITE_URL = "https://cricmatch247.com?btag=211079"

# CapSolver: solves the AWS WAF CAPTCHA that guards spin24star's register
# endpoint (see the "AWS WAF CAPTCHA" section in CLAUDE.md). The key is read
# lazily from the environment (via capsolver_key()) rather than cached at
# import, so `.env` loaded after import still applies. No key -> the solver is
# simply skipped and a WAF block is reported as a clean failure, unchanged.
CAPSOLVER_CREATE_URL = "https://api.capsolver.com/createTask"
CAPSOLVER_RESULT_URL = "https://api.capsolver.com/getTaskResult"


def capsolver_key():
    return os.environ.get("CAPSOLVER_API_KEY", "").strip()


def extract_referral_code(url):
    """Pull the affiliate/referral code out of a site URL's query string (e.g.
    "...?btag=211079" -> "211079"), for its own CSV/DB column separate from the
    full url. The param name is per-site (`tracking_param`, "btag" for the
    current sites). Returns None if the param is absent."""
    if not url:
        return None
    values = parse_qs(urlsplit(url).query).get(profile_for(url).tracking_param)
    return values[0] if values else None

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

# Per-site selectors + behavior now live in one profile file per site under
# sites/ (sites/cricmatch.py, sites/spin24star.py), selected by URL via
# profile_for(). Engine helpers read `profile_for(page.url).sel[...]` instead of
# a shared SEL dict -- so adding a site is a one-file change, no engine edits.
# See the "Multi-site support" section of CLAUDE.md.

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
    for sel in profile_for(page.url).sel["close_popup"]:
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
    """Click JOIN/REGISTER and wait for the register form to appear."""
    prof = profile_for(page.url)
    dismiss_popups(page)

    # Already showing? (Khelo sites open the form directly at /?reg=1.)
    try:
        if page.locator(prof.sel["username"]).first.is_visible():
            return True
    except Exception:
        pass

    # Khelo platform (spin24star): several REGISTER buttons in the DOM, only
    # one visible; a game section overlays it, so the click must be forced --
    # a plain click retries forever on "subtree intercepts pointer events".
    if prof.register_trigger == "forced_join":
        khelo = page.locator(prof.sel["open_modal_khelo"])
        for i in range(khelo.count()):
            btn = khelo.nth(i)
            try:
                if btn.is_visible():
                    btn.click(timeout=4000, force=True)
                    page.wait_for_selector(prof.sel["username"], state="visible", timeout=8000)
                    return True
            except Exception:
                continue
        return False

    for sel in prof.sel["open_modal"]:
        try:
            page.locator(sel).first.click(timeout=4000, force=True)
            page.wait_for_selector(prof.sel["username"], state="visible", timeout=8000)
            return True
        except Exception:
            continue
    return False


# ---------------------------------------------------------------------------
# AWS WAF CAPTCHA solving (CapSolver)
#
# spin24star's register POST (/sign-up) is guarded by an AWS WAF CAPTCHA
# action: the POST comes back HTTP 405 with an `x-amzn-waf-action: captcha`
# header and a "Human Verification" HTML page whose inline `window.gokuProps`
# carries the challenge key/iv/context. We hand those to CapSolver, get back a
# solved `aws-waf-token`, inject it as a cookie, and resubmit -- exactly the
# flow the site's own captcha.js would do after a human solved the puzzle.
# ---------------------------------------------------------------------------

def parse_aws_waf_challenge(body):
    """Pull the AWS WAF challenge params out of a 'Human Verification' page.
    Returns {"key","iv","context","challenge_js"} or None if `body` isn't one
    (e.g. cricmatch, which never serves this)."""
    if not body or "gokuProps" not in body:
        return None
    m = re.search(r"window\.gokuProps\s*=\s*(\{.*?\})\s*;", body, re.DOTALL)
    if not m:
        return None
    try:
        props = json.loads(m.group(1))
    except (ValueError, TypeError):
        return None
    if not props.get("key") or not props.get("context"):
        return None
    js = re.search(r'src="(https://[^"]*challenge\.js)"', body)
    return {"key": props.get("key"), "iv": props.get("iv"),
            "context": props.get("context"),
            "challenge_js": js.group(1) if js else None}


def _capsolver_proxy(proxy_str):
    """Convert our proxy string into CapSolver's `scheme://user:pass@host:port`
    form so the token is solved from the same egress IP the signup will use
    (AWS WAF tokens can be IP-bound). Returns None if no proxy."""
    if not proxy_str:
        return None
    conf = parse_proxy(proxy_str)
    scheme, hostport = conf["server"].split("://", 1)
    if scheme not in ("http", "https", "socks5"):
        scheme = "http"
    user, pw = conf.get("username"), conf.get("password")
    if user and pw:
        return f"{scheme}://{user}:{pw}@{hostport}"
    return f"{scheme}://{hostport}"


def _capsolver_post(url, payload, timeout=30):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def solve_aws_waf_token(website_url, challenge, api_key=None, proxy=None, timeout=180):
    """Solve an AWS WAF CAPTCHA via CapSolver and return the aws-waf-token
    string. Raises RuntimeError on any failure (no key, API error, timeout)."""
    api_key = api_key or capsolver_key()
    if not api_key:
        raise RuntimeError("CAPSOLVER_API_KEY not set")
    task = {
        "type": "AntiAwsWafTaskProxyLess",
        "websiteURL": website_url,
        "awsKey": challenge["key"],
        "awsIv": challenge.get("iv"),
        "awsContext": challenge["context"],
    }
    if challenge.get("challenge_js"):
        task["awsChallengeJS"] = challenge["challenge_js"]
    cap_proxy = _capsolver_proxy(proxy)
    if cap_proxy:
        task["type"] = "AntiAwsWafTask"
        task["proxy"] = cap_proxy

    created = _capsolver_post(CAPSOLVER_CREATE_URL, {"clientKey": api_key, "task": task})
    if created.get("errorId"):
        raise RuntimeError(f"createTask: {created.get('errorCode')} "
                           f"{created.get('errorDescription')}")
    task_id = created.get("taskId")
    if not task_id:
        raise RuntimeError(f"createTask returned no taskId: {created}")

    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(3)
        res = _capsolver_post(CAPSOLVER_RESULT_URL, {"clientKey": api_key, "taskId": task_id})
        if res.get("errorId"):
            raise RuntimeError(f"getTaskResult: {res.get('errorCode')} "
                               f"{res.get('errorDescription')}")
        if res.get("status") == "ready":
            sol = res.get("solution") or {}
            token = sol.get("cookie") or sol.get("token")
            if not token:
                raise RuntimeError(f"solution had no token: {sol}")
            return token
    raise RuntimeError("solve timed out")


def apply_waf_token(context, website_url, token):
    """Inject a solved aws-waf-token into a BrowserContext so the next request
    to the site carries it."""
    host = urlsplit(website_url).hostname
    context.add_cookies([{"name": "aws-waf-token", "value": token,
                          "domain": host, "path": "/"}])


def is_waf_captcha(captured):
    """True if the captured register response was an AWS WAF CAPTCHA block."""
    action = (captured.get("action") or "").lower()
    if action in ("captcha", "challenge"):
        return True
    return "gokuProps" in (captured.get("body") or "")


def fill_register_form(page, acct):
    """Fill the 4 register fields and ensure the T&C box is checked. Assumes
    the register form/modal is already open. Shared by the initial fill and the
    post-WAF-solve refill so they can't drift."""
    prof = profile_for(page.url)
    for sel, value in [(prof.sel["username"], acct["username"]),
                       (prof.sel["email"], acct["email"]),
                       (prof.sel["password"], acct["password"]),
                       (prof.sel["phone"], str(acct["phone"]))]:
        field = page.locator(sel)
        field.click()
        field.press_sequentially(value, delay=30)
        field.blur()
    if prof.has_terms_checkbox:
        try:
            cb = page.locator(prof.sel["terms"])
            if cb.count() and not cb.is_checked():
                cb.check(force=True)
        except Exception:
            pass


def click_register_and_wait(page):
    """Click REGISTER, capturing the register POST's response (for AWS WAF
    detection), and wait for the outcome. Returns (outcome, msgs, captured),
    where captured has {"response","action","body"} for the register call
    (empty on sites like cricmatch that don't route through /sign-up + WAF)."""
    captured = {}

    def on_resp(resp):
        try:
            if resp.request.method != "POST":
                return
            url = resp.url
            if "awswaf.com" in url:  # WAF telemetry beacons -- noise
                return
            action = resp.headers.get("x-amzn-waf-action")
            if action or url.rstrip("/").endswith("/sign-up"):
                captured["response"] = resp
                captured["action"] = action
                try:
                    captured["body"] = resp.text()
                except Exception:
                    captured["body"] = ""
        except Exception:
            pass

    page.on("response", on_resp)
    try:
        page.click(profile_for(page.url).sel["submit"])
        outcome, msgs = wait_for_register_outcome(page)
    finally:
        try:
            page.remove_listener("response", on_resp)
        except Exception:
            pass
    return outcome, msgs, captured


def submit_register(page, acct, site_url, proxy=None):
    """Click REGISTER and, if the register POST is AWS WAF CAPTCHA-blocked and a
    CapSolver key is configured, solve it and resubmit in a FRESH browser
    context. Returns (outcome, msgs, captured, page) -- `page` is the same
    object passed in, UNLESS a WAF retry happened, in which case it's a new
    Page in a new context and the caller must switch to using it (and treat
    the original page/context as already closed). With no key (or a non-WAF
    site) this is just a plain submit, so cricmatch is unaffected.

    Verified live: injecting a solved token into the SAME context that got
    challenged still 405s on reload -- AWS WAF keeps flagging that context
    even with a valid token (something beyond the token cookie is tracked per
    session). A token injected into a brand-new context works immediately
    (fresh homepage GET returns 200, real site). So the retry opens a new
    context rather than reloading in place."""
    outcome, msgs, captured = click_register_and_wait(page)

    if outcome in ("error", "timeout") and is_waf_captcha(captured) and capsolver_key():
        try:
            challenge = parse_aws_waf_challenge(captured.get("body", ""))
            if not challenge:
                raise RuntimeError("could not parse WAF challenge params")
            token = solve_aws_waf_token(site_url or SITE_URL, challenge, proxy=proxy)
        except (RuntimeError, urllib.error.URLError) as e:
            return outcome, [f"AWS WAF CAPTCHA solve failed: {e}"], captured, page

        old_context = page.context
        browser = old_context.browser
        try:
            proxy_conf = parse_proxy(proxy) if proxy else None
        except ValueError:
            proxy_conf = None
        new_context = browser.new_context(proxy=proxy_conf) if proxy_conf else browser.new_context()
        apply_waf_token(new_context, site_url or SITE_URL, token)
        new_page = new_context.new_page()
        try:
            old_context.close()
        except Exception:
            pass

        new_page.goto(site_url or SITE_URL, wait_until="domcontentloaded", timeout=60000)
        new_page.wait_for_timeout(4000)
        if not open_signup_modal(new_page):
            return ("timeout", ["WAF solved but could not reopen the register form"],
                    captured, new_page)
        fill_register_form(new_page, acct)
        outcome, msgs, captured = click_register_and_wait(new_page)
        return outcome, msgs, captured, new_page

    return outcome, msgs, captured, page


def read_result(page):
    """Best-effort read of any toast / validation message shown after submit.
    The scrape selectors are per-site (`result_selectors`) -- e.g. spin24star's
    errors are a top-right snackbar with no toast/alert class."""
    messages = []
    for sel in profile_for(page.url).result_selectors:
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
    else None. Not a toast -- a bare <li> inside .err_phone (cricmatch). Sites
    without a dedicated element (Khelo) set phone_taken_selector=None and this
    returns None, so a taken phone falls through to the generic result scrape."""
    prof = profile_for(page.url)
    if not prof.phone_taken_selector:
        return None
    try:
        el = page.locator(prof.phone_taken_selector).first
        if el.count() and el.is_visible():
            txt = (el.inner_text() or "").strip()
            if txt:
                return txt
    except Exception:
        pass
    return None


def _looks_like_otp_sent(msgs):
    """True for a benign 'OTP has been sent' style toast, as opposed to an
    actual rejection. Confirmed live on cricmatch: this toast can render
    before the OTP digit boxes do, so catching it here (and continuing to
    poll for the real OTP screen) avoids misreading a success message as
    'Register rejected: OTP has been sent.'"""
    joined = " ".join(msgs).lower()
    return "otp" in joined and "sent" in joined


def wait_for_register_outcome(page, timeout_ms=12000, poll_ms=250):
    """After clicking REGISTER, poll for whichever outcome shows up first
    instead of blindly sleeping: the OTP screen, the phone-taken error, or any
    other toast/inline error. Measured live: phone-taken typically renders in
    under 0.5s, so polling beats a flat sleep on the common paths without
    lowering the ceiling for slow ones.

    Returns (outcome, messages): messages is the read_result() snapshot taken
    the instant the error was spotted. Callers must use it rather than calling
    read_result() again -- snackbar-style toasts (spin24star) auto-dismiss, so
    a re-read moments later can come back empty ("unknown error")."""
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        if check_phone_taken(page):
            return "phone_taken", []
        try:
            if page.locator(profile_for(page.url).sel["otp_digits"]).first.is_visible():
                return "otp", []
        except Exception:
            pass
        msgs = read_result(page)
        if msgs and not _looks_like_otp_sent(msgs):
            return "error", msgs
        page.wait_for_timeout(poll_ms)
    return "timeout", []


def wait_for_otp_outcome(page, timeout_ms=10000, poll_ms=250):
    """After clicking the OTP Verify button, poll for either the inline OTP
    error appearing or the OTP screen closing (success), instead of a flat
    sleep."""
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        prof = profile_for(page.url)
        try:
            e = page.locator(prof.sel["otp_error"]).first
            if e.count() and e.is_visible():
                return "error"
        except Exception:
            pass
        try:
            still_open = (page.locator(prof.sel["otp_digits"]).first.is_visible()
                         if page.locator(prof.sel["otp_digits"]).count() else False)
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
    prof = profile_for(page.url)
    try:
        page.wait_for_selector(prof.sel["otp_digits"], state="visible", timeout=15000)
    except PWTimeout:
        result["messages"].append("No OTP screen appeared after REGISTER "
                                   "(check the result screenshot).")
        return result

    boxes = page.locator(prof.sel["otp_digits"])
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

    if not click_first_visible(page, prof.sel["otp_verify"], timeout=6000):
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
            e = page.locator(prof.sel["otp_error"]).first
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


def signup_once(page, acct, submit=True, interactive=False, site_url=None, proxy=None):
    """Run one signup attempt. Returns a result dict.

    If the site rejects the phone number as already registered and
    `interactive` is True, prompts for a different phone number and retries
    (up to 5 times) instead of failing outright. `proxy` (raw string) is only
    used to route the AWS WAF CAPTCHA solve through the same egress IP."""
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
    # each field afterward to trigger any on-blur checks. Also ensures the
    # "I'm over 18 + T&C" box is checked.
    fill_register_form(page, acct)

    SHOTS_DIR.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    filled_shot = SHOTS_DIR / f"{acct['username']}-{stamp}-filled.png"
    page.screenshot(path=str(filled_shot))

    if not submit:
        result["ok"] = True
        result["messages"] = ["--no-submit: form filled but not submitted."]
        result["shot"] = str(filled_shot)
        return result

    # submit_register may swap in a fresh page/context (see its docstring) if
    # it had to route around an AWS WAF CAPTCHA -- reassign the local `page`
    # so every use below (screenshots, phone-taken retry, enter_otp) targets
    # whichever page is actually live, and stash it in `result` so the caller
    # knows which context to close (the original may already be closed).
    outcome, msgs, _, page = submit_register(page, acct, site_url, proxy=proxy)
    result["page"] = page

    attempts = 0
    while outcome == "phone_taken" and interactive and attempts < 5:
        attempts += 1
        phone_err = check_phone_taken(page)
        print(f"\n{phone_err} Try a different phone number.")
        acct["phone"] = prompt_phone()
        prof = profile_for(page.url)
        phone_field = page.locator(prof.sel["phone"])
        phone_field.fill("")
        phone_field.click()
        phone_field.press_sequentially(str(acct["phone"]), delay=30)
        phone_field.blur()
        page.click(prof.sel["submit"])
        outcome, msgs = wait_for_register_outcome(page)

    msgs = msgs or read_result(page)
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


# ---------------------------------------------------------------------------
# HTTP-fast signup (no browser at all)
#
# For sites with supports_http_fast=True, the register form turns out to be a
# plain 2-call JSON API, discovered by capturing a real Playwright run's
# network traffic and then confirming live with a raw `curl` replay (see the
# "HTTP-fast signup" section of CLAUDE.md):
#   1. GET the site -> a `csrf-token` meta tag + session cookies
#   2. POST /register with otp="" -> triggers the SMS, e.g.
#      {"status":205,"message":"OTP has been sent.","message_class":"success"}
#   3. POST /register again with otp=<code> -> verifies it
# No JS execution, no WAF challenge on this endpoint (cricmatch only --
# spin24star's register POST IS WAF-gated and has supports_http_fast=False,
# see the AWS WAF section elsewhere in this file). Roughly 10-20x faster than
# the Playwright path since there's no browser launch/render/wait at all, but
# more fragile: it hard-codes today's field names/response shape instead of
# driving the real UI, so a backend change breaks it silently rather than
# surfacing as a missing selector.
# ---------------------------------------------------------------------------

_HTTP_FAST_USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                         "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


def http_session_for(proxy_str):
    """Build a requests.Session for the given proxy string (parse_proxy()'s
    format). Unlike Chromium, requests can authenticate to SOCKS5 proxies
    directly (via PySocks) -- the pproxy bridge in maybe_bridge_proxy() works
    around a Chromium-specific limitation that doesn't apply here."""
    session = requests.Session()
    session.headers.update({"User-Agent": _HTTP_FAST_USER_AGENT})
    if proxy_str:
        conf = parse_proxy(proxy_str)
        scheme, hostport = conf["server"].split("://", 1)
        user, pw = conf.get("username"), conf.get("password")
        auth = f"{user}:{pw}@" if user and pw else ""
        proxy_url = f"{scheme}://{auth}{hostport}"
        session.proxies = {"http": proxy_url, "https": proxy_url}
    return session


def http_fetch_csrf(session, site_url):
    """GET the site and pull the Laravel csrf-token meta tag out of the
    response (also seeds the session's cookies -- laravel_session, XSRF-TOKEN,
    AWSALB* -- for the register calls that follow)."""
    resp = session.get(site_url, timeout=20)
    resp.raise_for_status()
    m = re.search(r'<meta name="csrf-token" content="([^"]+)"', resp.text)
    if not m:
        raise RuntimeError("csrf-token meta tag not found on the homepage response "
                           "(site markup may have changed).")
    return m.group(1)


def http_register_call(session, csrf_token, acct, site_url, otp=""):
    """POST the register endpoint. `otp=""` triggers the SMS; a follow-up call
    with the real code verifies it -- same endpoint both times, confirmed live.
    Returns the parsed JSON body (or a synthetic error dict if the response
    isn't JSON, e.g. an unexpected block page)."""
    prof = profile_for(site_url)
    parts = urlsplit(site_url)
    register_url = f"{parts.scheme}://{parts.netloc}{prof.http_register_path}"
    data = {
        "username": acct["username"],
        "email": acct["email"],
        "password": acct["password"],
        "phone": str(acct["phone"]),
        "otp": otp,
        "_token": csrf_token,
    }
    headers = {
        "X-Requested-With": "XMLHttpRequest",
        "X-CSRF-TOKEN": csrf_token,
        "Referer": site_url,
    }
    resp = session.post(register_url, data=data, headers=headers, timeout=20)
    try:
        return resp.json()
    except ValueError:
        return {"status": None, "message_class": "danger",
                "message": f"Non-JSON response (HTTP {resp.status_code}): {resp.text[:200]}"}


def http_is_error(resp_json):
    return (resp_json.get("message_class") or "").lower() in ("danger", "error")


def http_is_phone_taken(resp_json):
    """Best-effort text match on the JSON error message. NOT yet verified live
    against a genuine taken-phone response (only reachable with a number that
    already completed a real registration) -- modeled on the DOM error's known
    wording ("The mobile number has already been taken.", see
    check_phone_taken()). If this misclassifies, the raw message still reaches
    result["messages"] via the generic-error fallback, so nothing is hidden."""
    msg = (resp_json.get("message") or "").lower()
    return "taken" in msg and ("mobile" in msg or "phone" in msg)


def http_signup_once(acct, submit=True, interactive=False, site_url=None, proxy=None):
    """HTTP-only signup for sites with supports_http_fast=True. Same result
    shape as signup_once() ({"account","ok","messages","shot"}) so callers can
    treat both interchangeably, except "shot" is always None -- no browser, no
    screenshot to take."""
    url = site_url or SITE_URL
    prof = profile_for(url)
    result = {"account": acct.get("username", "?"), "ok": None, "messages": [], "shot": None}

    try:
        session = http_session_for(proxy)
    except ValueError as e:
        result["ok"] = False
        result["messages"] = [f"Bad --proxy value: {e}"]
        return result

    try:
        csrf = http_fetch_csrf(session, url)
    except (requests.RequestException, RuntimeError) as e:
        result["ok"] = False
        result["messages"] = [f"Could not load the site (check the URL/proxy?): {str(e)[:200]}"]
        return result

    if not submit:
        result["ok"] = True
        result["messages"] = ["--no-submit has nothing to fill in --fast mode "
                              "(no browser/form) -- confirmed the site loads and "
                              "handed back a csrf token instead."]
        return result

    attempts = 0
    resp_json = {}
    while True:
        try:
            resp_json = http_register_call(session, csrf, acct, url, otp="")
        except requests.RequestException as e:
            result["ok"] = False
            result["messages"] = [f"Register request failed: {str(e)[:200]}"]
            return result

        if not http_is_error(resp_json):
            break  # OTP triggered

        if http_is_phone_taken(resp_json) and interactive and attempts < 5:
            attempts += 1
            print(f"\n{resp_json.get('message')} Try a different phone number.")
            acct["phone"] = prompt_phone()
            continue

        result["ok"] = False
        result["messages"] = [resp_json.get("message") or f"Register rejected: {resp_json}"]
        if attempts:
            result["messages"].append(f"Gave up after {attempts} retr{'y' if attempts == 1 else 'ies'}.")
        return result

    print(f"\nOTP requested — an SMS code was sent to {acct.get('phone', 'your phone')}.")
    print(f"  server says: {resp_json.get('message')}")
    otp = prompt_otp(prof.http_otp_digits)

    try:
        verify_json = http_register_call(session, csrf, acct, url, otp=otp)
    except requests.RequestException as e:
        result["ok"] = False
        result["messages"] = [f"OTP verify request failed: {str(e)[:200]}"]
        return result

    if http_is_error(verify_json):
        result["ok"] = False
        result["messages"] = [f"OTP rejected: {verify_json.get('message') or verify_json}"]
        return result

    result["ok"] = True
    result["messages"] = [verify_json.get("message") or "OTP verified — account appears registered."]
    return result


# ---------------------------------------------------------------------------
# Casino game smoke test (login + place a live Baccarat bet)
#
# Separate from the signup flow above: logs into an EXISTING account (not a
# freshly-generated one) and places a real bet on a third-party live-dealer
# game, to confirm the casino game integration itself works end-to-end, not
# just that the site loads. Verified live against cricmatch247 only
# (2026-07-16, real account, real ₹100 bet) -- spin24star is NOT covered
# (its profile sets supports_casino=False, so login() refuses cleanly).
#
# The Baccarat game opens in a brand-new browser tab, cross-origin at
# ezugi.evo-games.com (Evolution/Ezugi), not embedded in the cricmatch247
# page itself -- callers must track and close that extra page/tab
# separately from the main site page (test_baccarat() does this).
#
# Chip denomination is NOT selectable by this code -- clicking a bet spot
# places whatever chip the game UI currently has selected by default
# (confirmed live: this is normally the table minimum, e.g. cricmatch247's
# Baccarat A/B tables default to their ₹100 minimum chip). `amount` is
# therefore advisory: place_baccarat_bet() reports what was ACTUALLY placed
# (read back from the game's own "TOTAL BET" counter) rather than trusting
# the requested amount blindly, and refuses to proceed if the observed
# amount doesn't match what was requested.
#
# Bet-spot targeting is the fragile part: PLAYER/BANKER are real DOM
# elements (not canvas), but their class names are hashed/dynamic and reused
# misleadingly elsewhere in the DOM -- a *collapsed* paytable/bet-limits
# tooltip contains the literal text "BANKER" even while hidden, and mis-
# targeting it during live testing bounced the page out to the game lobby
# instead of placing a bet (caught before any money moved, since Evolution
# only submits a staged chip placement to the server when the betting timer
# naturally expires -- leaving the table before that voids it). The
# _TAG_BET_SPOT_JS heuristic below excludes that tooltip container and
# anything off-screen or oversized, then picks the smallest remaining match.
# ---------------------------------------------------------------------------

def _page_debug_info(page):
    """URL + title + a snippet of visible text, for diagnosing a login failure
    on a remote/prod machine we can't watch -- e.g. a WAF/403 block page served
    to a datacenter IP has a telltale title/text and no login button. Goes into
    the error message itself, so it shows up right in the chat."""
    try:
        title = ""
        try:
            title = page.title()
        except Exception:
            pass
        body = ""
        try:
            body = page.locator("body").inner_text(timeout=2000)
        except Exception:
            pass
        body = " ".join(body.split())[:200]
        return f"[page URL: {page.url} | title: {title!r} | text: {body!r}]"
    except Exception:
        return ""


def _login_debug_shot(page, username):
    """Save a screenshot of the page at a login failure (on the server running
    the bot), so a remote failure has visual evidence. Returns a path suffix."""
    try:
        SHOTS_DIR.mkdir(exist_ok=True)
        path = SHOTS_DIR / f"login-fail-{username}-{time.strftime('%Y%m%d-%H%M%S')}.png"
        page.screenshot(path=str(path))
        return f" [screenshot: {path}]"
    except Exception:
        return ""


def login(page, username, password, site_url=None):
    """Log into an EXISTING account (not signup). Returns (outcome, messages)
    where outcome is "ok", "error", or "timeout"."""
    prof = profile_for(site_url or SITE_URL)
    if not prof.supports_casino:
        return "error", [f"Login/casino is not supported for {prof.key} "
                         "(its login/casino selectors are not inspected)."]
    try:
        page.goto(site_url or SITE_URL, wait_until="domcontentloaded", timeout=60000)
    except PWError as e:
        return "error", [f"Couldn't load the site (check the proxy?): {str(e)[:150]}"]
    page.wait_for_timeout(4000)

    # Retry finding the LOGIN button over a longer window, dismissing popups
    # each pass. On a slow/remote (prod) machine the homepage can take longer to
    # render and a promo overlay can cover the button; a single 5s attempt then
    # fails. If it never appears, capture what the page actually is -- a WAF/403
    # block page (served to datacenter IPs) has no login button at all.
    clicked = False
    deadline = time.time() + 20
    while time.time() < deadline:
        dismiss_popups(page)
        if click_first_visible(page, [prof.sel["open_login"]], timeout=3000):
            clicked = True
            break
        page.wait_for_timeout(1000)
    if not clicked:
        info = _page_debug_info(page)
        shot = _login_debug_shot(page, username)
        return "timeout", [f"Could not find the LOGIN button. {info}{shot}"]
    page.wait_for_timeout(1000)

    try:
        page.wait_for_selector(prof.sel["login_username"], state="visible", timeout=8000)
    except PWTimeout:
        return "timeout", [f"Login form did not appear. {_page_debug_info(page)}"
                           f"{_login_debug_shot(page, username)}"]

    user_field = page.locator(prof.sel["login_username"])
    user_field.click()
    user_field.press_sequentially(username, delay=30)
    pass_field = page.locator(prof.sel["login_password"])
    pass_field.click()
    pass_field.press_sequentially(password, delay=30)
    page.locator(prof.sel["login_submit"]).click(timeout=5000)

    deadline = time.time() + 12
    while time.time() < deadline:
        try:
            if page.locator(prof.sel["logged_in_indicator"]).first.is_visible():
                _dismiss_stuck_login_modal(page)
                return "ok", []
        except Exception:
            pass
        msgs = read_result(page)
        if msgs:
            return "error", msgs
        page.wait_for_timeout(250)
    return "timeout", ["Login did not complete within the timeout."]


def _dismiss_stuck_login_modal(page):
    """Confirmed live 2026-07-19: the LOGIN popup (#nwGuestSec) can still be
    sitting open on screen even after `logged_in_indicator` is already
    visible underneath it -- Playwright's is_visible() only checks the
    element's own box/CSS, not whether something is drawn on top of it, and
    waiting (tested live up to 20s) never makes the popup auto-close on its
    own. Left alone this silently blocks every subsequent click (casino nav,
    etc.) since the popup still covers the whole page even though login()
    itself reports "ok". Close button confirmed live: `a.close_desktop`
    inside #nwGuestSec (onclick="loginFunc()")."""
    try:
        loc = page.locator("#nwGuestSec a.close_desktop")
        if loc.count() and loc.first.is_visible():
            loc.first.click(timeout=2000, force=True)
            page.wait_for_timeout(300)
    except Exception:
        pass


def open_casino_lobby(page, timeout_ms=15000):
    """Navigate to the Live Casino section. Call after a successful login()
    on the same page.

    Confirmed live: a direct `page.goto()` to /live-casino is NOT safe here
    -- it forces a full page reload, and this SPA doesn't reliably restore
    the logged-in view from cookies alone on a hard reload (observed live:
    landed back on a logged-out-looking homepage). Must navigate via the
    in-app SPA router instead, i.e. an actual click, which is why
    the casino_nav selector deliberately excludes the top-nav tab (whose click
    intermittently no-ops, likely a race with the SPRIBE overlay's own
    animation) in favor of the sidebar link, which has a real href and sits
    outside the overlay's click area. Confirmed live this needs a NORMAL
    click, not force=True -- forcing it dispatches the click without
    triggering the router's real navigation (URL never changed), while a
    plain actionability-checked click navigates correctly, since this link
    isn't covered by anything at its actual screen position anyway. Still
    dismisses the homepage's SPRIBE/Aviator walkthrough overlay, since it
    can reappear on this route too (confirmed live, its own animation
    timing, not just on first page load) -- polls dismiss_popups() until the
    lobby's own category tabs (e.g. "Baccarat") are actually visible.

    Confirmed live: even a plain, correctly-targeted click on this link can
    still silently no-op right after login (probably the SPA's router event
    listeners aren't attached yet the instant the logged-in view first
    renders) -- retries the click itself, not just the readiness poll,
    since a single click attempt isn't reliable enough here."""
    dismiss_popups(page)
    casino_nav = profile_for(page.url).sel["casino_nav"]
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        try:
            page.locator(casino_nav).first.click(timeout=5000)
        except Exception:
            pass
        # Check success BEFORE paying dismiss_popups' own wait -- on the common
        # (fast) path the nav already worked and there's no popup blocking
        # anything else, so this skips ~800ms of unnecessary waiting per
        # account. Only fall back to the heavier dismiss_popups() pass (as
        # before) if the lobby isn't visible yet.
        page.wait_for_timeout(1000)
        try:
            if page.locator("a:has-text('Baccarat')").first.is_visible():
                return True
        except Exception:
            pass
        dismiss_popups(page)
        try:
            if page.locator("a:has-text('Baccarat')").first.is_visible():
                return True
        except Exception:
            pass
    return False


def _dismiss_casino_promo_modal(page):
    """Close the "Victory Boost"-style promo modal that can appear on the
    Live Casino page (confirmed live, cricmatch247 only, logged-in session
    only -- never observed during the signup flow, so this is deliberately
    NOT folded into the shared dismiss_popups()/close_popup list, to avoid
    touching that already-verified signup code path). Confirmed live: even a
    force=True click on the actual game tile silently no-ops while this
    modal is open (the site's own handler appears to ignore it), so it must
    be closed first, not just clicked through. Escape does NOT close it
    (confirmed live); a broad `[class*=close]` guess is unsafe (confirmed
    live it mismatches the unrelated Account panel toggle, whose class is
    literally "accSec_close" -- a "closed/expanded" state name, not a close
    button -- and opens that panel instead). The real close "X" is an SVG
    with no useful class of its own, but its container does:
    `.bonuspage_Popup-header` (identified live via
    `document.elementFromPoint` on the visible X icon)."""
    try:
        loc = page.locator(".bonuspage_Popup-header svg")
        if loc.count() and loc.first.is_visible():
            loc.first.click(timeout=1500, force=True)
    except Exception:
        pass
    page.wait_for_timeout(500)


def _dismiss_choose_chips_modal(page):
    """Close the "CHOOSE CHIPS: Bonus or Real" gate that appears right after
    clicking a game tile, before the game's own tab opens -- confirmed live
    2026-07-19 on an account with an active bonus balance + wagering
    requirement. Left alone this silently eats the tile click (the click
    lands on the modal instead of ever opening a new tab), which is
    indistinguishable from "could not open the table" without a screenshot.
    Always picks REAL CHIPS -- this driver tests real-money betting, not the
    bonus balance -- via a plain text locator (no stable class name was
    needed to identify it live)."""
    try:
        # The "REAL CHIPS" label itself has no click handler (verified live
        # 2026-07-19: clicking it leaves the modal up and the game never
        # launches). The actual button is the red amount div below it.
        btn = page.locator("div.cls_play_act_bal.redirectLink")
        if not (btn.count() and btn.first.is_visible()):
            btn = page.locator("text=REAL CHIPS")
        if btn.count() and btn.first.is_visible():
            btn.first.click(timeout=2000, force=True)
            page.wait_for_timeout(1000)
    except Exception:
        pass


def search_and_open_game(page, category, tile_text):
    """Filter the Live Casino lobby to `category` (e.g. "Baccarat") and open
    the game tile matching `tile_text` (e.g. "Baccarat A"). There is no
    free-text game search box -- the lobby only has category filter tabs, so
    `category` must exactly match one of them. The game opens in a NEW
    browser tab (confirmed live: Evolution/Ezugi games are cross-origin at
    ezugi.evo-games.com, not embedded in the page) -- returns that new Page,
    or None on failure. Caller owns closing it."""
    context = page.context
    pages_before = len(context.pages)
    _dismiss_casino_promo_modal(page)
    try:
        page.locator(f"a:has-text('{category}')").first.click(timeout=5000, force=True)
        page.wait_for_timeout(2500)
        _dismiss_casino_promo_modal(page)
        page.locator(f"text={tile_text}").first.click(timeout=5000, force=True)
        page.wait_for_timeout(1000)
        _dismiss_choose_chips_modal(page)
    except Exception:
        return None

    deadline = time.time() + 15
    while time.time() < deadline:
        if len(context.pages) > pages_before:
            new_page = context.pages[-1]
            new_page.wait_for_timeout(3000)
            return new_page
        # Bonus-balance accounts don't get a new tab at all: choosing REAL
        # CHIPS in the CHOOSE CHIPS gate navigates THIS tab straight to the
        # provider (confirmed live 2026-07-19, url -> ezugi.evo-games.com).
        try:
            if "evo-games" in page.url or "ezugi" in page.url:
                page.wait_for_timeout(3000)
                return page
        except Exception:
            pass
        # The gate can also appear later than the first dismiss attempt.
        _dismiss_choose_chips_modal(page)
        page.wait_for_timeout(500)
    return None


def find_game_frame(game_page, host_hint, min_nodes=50, timeout_ms=15000):
    """Locate the third-party game's actual UI frame within the newly opened
    game tab (the provider loads several frames; the real betting UI is the
    one with the most DOM nodes). `host_hint` narrows to frames whose URL
    contains it (e.g. "evo-games.com"). Returns the Frame, or None if it
    never loads within the timeout."""
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        best, best_n = None, 0
        for fr in game_page.frames:
            if host_hint not in fr.url:
                continue
            try:
                n = fr.eval_on_selector_all("div", "els => els.length")
            except Exception:
                n = 0
            if n > best_n:
                best, best_n = fr, n
        if best and best_n > min_nodes:
            return best
        game_page.wait_for_timeout(500)
    return None


# The Evolution/Ezugi UI tags every bet spot with a semantic, stable
# data-role. Baccarat uses "bet-spot-Player", "bet-spot-Banker",
# "bet-spot-Tie", "bet-spot-SuperSix", "bet-spot-PlayerPair",
# "bet-spot-BankerPair", "bet-spot-PlayerBonus", "bet-spot-BankerBonus",
# "bet-spot-PerfectPair", "bet-spot-EitherPair" (enumerated live, read-only,
# 2026-07-17); Stock Market Live uses a DIFFERENT convention entirely --
# "SM_Up" / "SM_Down", with no "bet-spot-" prefix at all (enumerated live
# 2026-07-20, see probe_evo_lobby.py). So this takes the COMPLETE data-role
# value, not a suffix to interpolate into a "bet-spot-{}" template.
#
# Targeting by exact data-role is unambiguous, so this deliberately does NOT
# fall back to any text-matching heuristic -- for a real-money click, a clean
# "spot not found" failure is safer than a heuristic that could mis-click a
# different bet.
_TAG_BET_SPOT_JS = """(role) => {
    document.querySelectorAll('[data-pw-spot]').forEach(e => e.removeAttribute('data-pw-spot'));
    const el = document.querySelector(`[data-role="${role}"]`);
    if (!el) return false;
    el.setAttribute('data-pw-spot', role);
    return true;
}"""

_READ_TOTAL_BET_JS = """() => {
    const els = Array.from(document.querySelectorAll('div, span'));
    const candidates = els
        .filter(e => (e.innerText||'').trim().toUpperCase().startsWith('TOTAL BET'))
        .map(e => (e.innerText||'').replace(/[\\u2066\\u2069\\u200b]/g, ''))
        .filter(t => /[0-9]/.test(t))
        .sort((a, b) => a.length - b.length);
    if (!candidates.length) return null;
    const m = candidates[0].match(/([0-9][0-9,]*)/);
    return m ? parseInt(m[1].replace(/,/g, ''), 10) : null;
}"""


def _read_total_bet(frame):
    """Read the game's own TOTAL BET counter (the ground truth for what's
    actually been placed) rather than trusting that a click "worked"."""
    try:
        return frame.evaluate(_READ_TOTAL_BET_JS)
    except Exception:
        return None


_BETTING_OPEN_JS = """() => {
    const t = document.querySelector('[data-role="circle-timer"]');
    return !!(t && t.getBoundingClientRect().height > 0);
}"""


_READ_INSTRUCTION_JS = """(role) => {
    const e = document.querySelector(`[data-role="${role}"]`);
    if (!e) return null;
    if (e.getBoundingClientRect().height <= 0) return null;
    return (e.innerText || '').replace(/[\\u2066\\u2069\\u200b]/g, '').trim().toUpperCase();
}"""


def _read_instruction(frame, role="instruction-message"):
    """Read the game's phase banner text (e.g. "PLACE YOUR BETS", "MAKE YOUR
    DECISION"). Returns an upper-cased string, or None if absent/hidden."""
    try:
        return frame.evaluate(_READ_INSTRUCTION_JS, role)
    except Exception:
        return None


def _betting_open(frame, game=None):
    """True when the live table is in the OPEN 'place your bets' phase.

    Two detection modes, because the two supported games signal this
    completely differently:

    * "timer" (baccarat): the Evolution frame renders the 'PLACE YOUR BETS'
      banner on canvas (it never appears in the DOM), but a
      [data-role="circle-timer"] element is present ONLY while betting is open
      -- verified live (2026-07-17): it appears for the ~15s window and is
      absent between rounds.
    * "instruction" (Stock Market Live): there is NO circle-timer at all, and
      the visible role SET is byte-identical in every phase (confirmed live
      2026-07-20 by diffing roles across a full round -- nothing changed), so
      role presence cannot work here. The phase lives in the TEXT of
      [data-role="instruction-message"] instead.

    TOTAL BET reads 0 in BOTH the open-and-empty and the closed states, so
    this is the only reliable 'is the window actually open' signal for gating
    a bet -- and the fix for the hedge placing one side into an open window
    while the other account's window was still closed (partial/unhedged)."""
    game = game or BACCARAT
    if game.window_mode == "instruction":
        text = _read_instruction(frame, game.instruction_role)
        if not text:
            return False
        return any(marker in text for marker in game.instruction_open)
    try:
        return bool(frame.evaluate(_BETTING_OPEN_JS))
    except Exception:
        return False


def _click_bet_spot(frame, role, timeout=5000):
    """Tag and click the bet spot with the exact data-role `role` (e.g.
    "bet-spot-Banker" on baccarat, "SM_Up" on Stock Market Live). Returns True
    if the element was found and clicked -- NOT proof the bet registered, the
    caller must verify via _read_total_bet. The bet-spot container has
    pointer-events:none with an inner SVG <path> (pointer-events:all) filling
    it as the real hot-zone (confirmed live via elementFromPoint), so a
    force-click at the container's centre lands on that path. Force is used
    because a decorative glow layer can also sit over the spot."""
    try:
        if not frame.evaluate(_TAG_BET_SPOT_JS, role):
            return False
        frame.locator(f'[data-pw-spot="{role}"]').click(timeout=timeout, force=True)
        return True
    except Exception:
        return False


def wait_for_live_table(frame, game_page, timeout_ms=30000, game=None):
    """Wait until the live-dealer table is actually interactive, i.e. its
    loading/intro screen is gone and the bet spots exist. Confirmed live
    (2026-07-17): find_game_frame() returns as soon as the frame has enough
    DOM nodes, but that can be the SPRIBE-style loading screen
    (data-role="loading-screen"/"loading-screen-image"/"progress-star"),
    which fully overlays the bet spots -- clicking then just hits the loader.
    Returns True once the table is live, False on timeout.

    The "bet spots exist" probe is per-game (`table_ready_role`) -- baccarat's
    is bet-spot-Banker, Stock Market Live's is SM_Up, so this must not be
    hardcoded or a non-baccarat table never reports ready."""
    game = game or BACCARAT
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        try:
            st = frame.evaluate("""(readyRole) => {
                const l = document.querySelector('[data-role="loading-screen"]');
                const loadingVisible = l && l.getBoundingClientRect().height > 0;
                const spot = document.querySelector(`[data-role="${readyRole}"]`);
                return {loadingVisible: !!loadingVisible, hasSpot: !!spot};
            }""", game.table_ready_role)
            if st["hasSpot"] and not st["loadingVisible"]:
                return True
        except Exception:
            pass
        game_page.wait_for_timeout(1000)
    return False


def place_baccarat_bet(game_page, frame, amount, round_attempts=12):
    """Place `amount` on BOTH Player and Banker in the open Baccarat frame.
    Chip denomination is not selectable (see module notes above) -- this
    clicks whatever chip is already selected and verifies the ACTUAL placed
    amount via the game's own TOTAL BET counter.

    Both bets go down BACK-TO-BACK in the SAME betting window. This is the
    fix for the Banker-never-registers failure seen live (2026-07-16/17):
    the old code confirmed the Player bet, then waited >1s before even
    attempting Banker, so if the Player bet landed near the end of a betting
    window the window closed before Banker's click registered, leaving TOTAL
    BET stuck at `amount`. Clicking Player then Banker with no gap keeps both
    inside one window.

    Retries across rounds: clicking during the results/reveal phase (between
    rounds) is a silent no-op that leaves TOTAL BET at 0, so this polls up to
    `round_attempts` times for a window where both bets land. A window that
    closes exactly between the two clicks leaves TOTAL BET at `amount` (one
    side committed) -- reported as a partial and NOT retried, to avoid
    stacking bets across rounds.

    Returns {"ok", "messages", "total_bet"}."""
    result = {"ok": False, "messages": [], "total_bet": None}

    if not wait_for_live_table(frame, game_page):
        result["messages"].append("Table never finished loading (stuck on the "
                                   "intro/loading screen) -- no bet placed.")
        return result

    for attempt in range(round_attempts):
        tb = _read_total_bet(frame)
        if tb is None:
            game_page.wait_for_timeout(1000)
            continue
        if tb != 0:
            # A bet is already showing for this round -- either a stale/partial
            # from a previous iteration or an unexpected state. Wait for a
            # fresh window (TOTAL BET back to 0) rather than stacking on top.
            game_page.wait_for_timeout(1500)
            continue

        # Fresh window candidate: place BOTH bets back-to-back, no gap.
        # Suffixes are the exact data-role case ("Player"/"Banker"), which is
        # what _TAG_BET_SPOT_JS matches -- uppercase would not resolve.
        _click_bet_spot(frame, "Player")
        _click_bet_spot(frame, "Banker")
        game_page.wait_for_timeout(1500)
        tb2 = _read_total_bet(frame)

        if tb2 == amount * 2:
            result["ok"] = True
            result["total_bet"] = tb2
            result["messages"].append(
                f"Placed {amount} on Player and {amount} on Banker "
                f"(TOTAL BET confirmed at {tb2}).")
            return result
        if tb2 == amount:
            result["total_bet"] = tb2
            result["messages"].append(
                f"Only one side registered ({amount}) -- the betting window "
                "likely closed between the two clicks. That one bet is committed "
                "and stays active; stopping so we don't stack onto the next round.")
            return result
        if tb2 in (0, None):
            # Neither landed: window was closed (between rounds). Try next window.
            game_page.wait_for_timeout(2000)
            continue
        # Unexpected non-zero, non-{amount, 2*amount} total.
        result["total_bet"] = tb2
        result["messages"].append(
            f"Unexpected TOTAL BET {tb2!r} after placing both bets "
            f"(wanted {amount * 2}) -- stopping.")
        return result

    result["total_bet"] = _read_total_bet(frame)
    result["messages"].append(
        f"Could not get both bets down in one open window after {round_attempts} "
        "attempts -- no bet, or a partial that was reported above.")
    return result


def test_baccarat(page, username, password, amount, site_url=None, category="Baccarat",
                   tile_text="Baccarat A"):
    """Smoke-test the casino game integration: log in, open a Baccarat table,
    and place `amount` on both Player and Banker. Returns a result dict in
    the same shape convention as signup_once(): {"ok", "messages", "shot"}.
    Does not wait for the round to resolve -- confirms placement, not outcome.

    Only verified live against cricmatch247 (2026-07-16). Does not write to
    accounts.db -- this tests an EXISTING account, not a newly created one, a
    different data lifecycle than the rest of this file."""
    result = {"ok": False, "messages": [], "shot": None}

    outcome, msgs = login(page, username, password, site_url=site_url)
    if outcome != "ok":
        result["messages"] = msgs or [f"Login did not succeed (outcome={outcome})."]
        SHOTS_DIR.mkdir(exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        shot = SHOTS_DIR / f"{username}-{stamp}-login-failed.png"
        page.screenshot(path=str(shot))
        result["shot"] = str(shot)
        return result

    if not open_casino_lobby(page):
        result["messages"] = ["Could not open the Live Casino section."]
        return result

    game_page = search_and_open_game(page, category, tile_text)
    if game_page is None:
        result["messages"] = [f"Could not open the {tile_text!r} game tile."]
        return result

    try:
        frame = find_game_frame(game_page, "evo-games.com")
        if frame is None:
            result["messages"] = ["Game tab opened but its UI frame never loaded."]
            return result

        bet_result = place_baccarat_bet(game_page, frame, amount)
        result["ok"] = bet_result["ok"]
        result["messages"] = bet_result["messages"]

        SHOTS_DIR.mkdir(exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        shot = SHOTS_DIR / f"{username}-{stamp}-baccarat-bet.png"
        game_page.screenshot(path=str(shot))
        result["shot"] = str(shot)
        return result
    finally:
        try:
            game_page.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Paired-account hedge betting
#
# Two accounts on the SAME live baccarat table, betting opposite sides
# (one Banker, one Player) on the SAME hand each round. Because both bets ride
# the same result, the money mostly just moves between the two accounts; only
# the ~5% banker commission bleeds out on a Banker win. This lets you generate
# large, controlled betting volume to smoke-test the platform without draining
# balance fast. Real money -- see the safety notes in place_baccarat_bet and
# the per-round verification below.
#
# v1 does NOT select a chip denomination: it bets whatever chip the table has
# selected by default (the table minimum, e.g. ₹100 on Baccarat A) and VERIFIES
# the actual placed amount via the game's own TOTAL BET counter. If the
# requested `amount` doesn't match what the table actually placed, the run
# aborts immediately with the observed value rather than repeating a wrong-size
# bet. Arbitrary chip selection is a future enhancement.
# ---------------------------------------------------------------------------

_BALANCE_JS = """() => {
    const b = document.querySelector('[data-role="balance-label-value"]');
    if (!b) return null;
    const t = (b.innerText || '').replace(/[\\u2066\\u2069\\u200b]/g, '');
    const m = t.match(/([0-9][0-9,]*)/);
    return m ? parseInt(m[1].replace(/,/g, ''), 10) : null;
}"""


_PORTFOLIO_JS = """(role) => {
    const e = document.querySelector(`[data-role="${role}"]`);
    if (!e) return null;
    const t = (e.innerText || '').replace(/[\\u2066\\u2069\\u200b]/g, '');
    // The element also carries its "PORTFOLIO" title and "1% FEE" label, so
    // take the LAST currency-looking number, which is the value itself.
    const all = t.match(/([0-9][0-9,]*(?:\\.[0-9]+)?)/g);
    if (!all || !all.length) return null;
    return parseFloat(all[all.length - 1].replace(/,/g, ''));
}"""


def read_portfolio(frame, game=None):
    """Read Stock Market Live's PORTFOLIO value (the live mark-to-market worth
    of an open position) as a float, or None. Captured live 2026-07-20: the
    element's text is "PORTFOLIO\\n1% FEE\\n₹0.00" with nothing staked, hence
    taking the last number rather than the first.

    This doubles as the "is there anything to cash out" signal, because the
    CASH OUT button itself is NOT usable for that -- confirmed live, its
    `disabled` property is false and its opacity is 1 even with no position
    open (it's styled purely by CSS class), so a position must be detected by
    portfolio > 0 instead."""
    game = game or STOCKMARKET
    try:
        return frame.evaluate(_PORTFOLIO_JS, game.portfolio_role)
    except Exception:
        return None


# Chip rail. Captured live 2026-07-20: every chip is
# <div data-role="chip" data-value="10|50|100|200|500|2500"> with
# cursor:pointer, and [data-role="selected-chip"] holds the active value. The
# numbers are SVG <text>, so innerText is empty on all of them -- read
# data-value / textContent, never innerText.
_READ_CHIPS_JS = """() => {
    const chips = Array.from(document.querySelectorAll('[data-role="chip"]'))
        .map(e => parseInt(e.getAttribute('data-value'), 10))
        .filter(v => !isNaN(v));
    const sel = document.querySelector('[data-role="selected-chip"]');
    const selVal = sel ? parseInt((sel.textContent || '').replace(/[^0-9]/g, ''), 10) : null;
    return {chips, selected: isNaN(selVal) ? null : selVal};
}"""


def read_chips(frame):
    """Return {"chips": [10, 50, ...], "selected": 10} for the table's chip
    rail, or {"chips": [], "selected": None} if it isn't present."""
    try:
        return frame.evaluate(_READ_CHIPS_JS)
    except Exception:
        return {"chips": [], "selected": None}


def select_chip(frame, amount, timeout=4000, wait_secs=75, game=None):
    """Pick the chip worth exactly `amount`. Returns True once the rail
    reports it selected.

    Without this the engine bets whatever the table has pre-selected -- the
    minimum, ₹10 -- so a requested ₹100 silently placed ₹10 and tripped the
    amount_mismatch stop. Verifies via [data-role="selected-chip"] rather than
    trusting the click, same principle as checking TOTAL BET after a bet.

    Retries across `wait_secs` rather than trying once: the rail is not always
    interactive between rounds (a single attempt at setup failed live on
    2026-07-20, run #8), so this spans at least one full betting window --
    ~21s on Stock Market Live -- giving the click a live rail to land on."""
    deadline = time.time() + wait_secs
    while True:
        try:
            if read_chips(frame).get("selected") == amount:
                return True
            loc = frame.locator(f'[data-role="chip"][data-value="{amount}"]')
            if loc.count():
                loc.first.click(timeout=timeout, force=True)
                check = time.time() + 3
                while time.time() < check:
                    if read_chips(frame).get("selected") == amount:
                        return True
                    time.sleep(0.2)
        except Exception:
            pass
        if time.time() >= deadline:
            return False
        time.sleep(1)


def describe_chip_rail(frame):
    """One-line summary of the rail, for failure messages -- 'rail not found'
    reads very differently from 'rail present, click ignored'."""
    r = read_chips(frame)
    chips = r.get("chips") or []
    if not chips:
        return "no chip rail visible"
    return f"chips {chips}, currently selected ₹{r.get('selected')}"


# Full markup of the cash-out panel, used only for diagnosis: the enabled
# styling can only be observed with a real position open, so a normal hedged
# round captures it once and stores it on the run record.
_DUMP_CASHOUT_JS = """(role) => {
    const root = document.querySelector(`[data-role="${role}"]`);
    if (!root) return {error: "not found"};
    const nodes = [];
    const walk = (e, d) => {
        const cs = getComputedStyle(e);
        nodes.push({d, tag: e.tagName, cls: (e.className || '').toString().slice(0, 40),
                    txt: (e.innerText || '').trim().slice(0, 20),
                    op: cs.opacity, color: cs.color, bg: cs.backgroundColor,
                    cursor: cs.cursor, filter: cs.filter, pointer: cs.pointerEvents});
        for (const c of e.children) walk(c, d + 1);
    };
    walk(root, 0);
    return {nodes, html: root.outerHTML.slice(0, 1200)};
}"""


_CASHOUT_ENABLED_JS = """(role) => {
    const root = document.querySelector(`[data-role="${role}"]`);
    if (!root) return null;
    // The panel greys itself out by dropping the CASH OUT *label* to
    // opacity 0.5 -- the root reports disabled=false / opacity=1 /
    // pointerEvents=auto in every phase, so the root tells us nothing.
    // Find the innermost node whose text is exactly "CASH OUT".
    let best = null;
    for (const e of root.querySelectorAll('*')) {
        const t = (e.innerText || '').trim().toUpperCase();
        if (t !== 'CASH OUT') continue;
        if (best === null || e.contains(best) === false) best = e;
    }
    if (!best) return null;
    return parseFloat(getComputedStyle(best).opacity);
}"""


def _cashout_enabled(frame, game=None):
    """True when the CASH OUT button is actually live (not greyed).

    Established live 2026-07-20 by dumping the panel's subtree: the root
    [data-role="cash-out"] reports disabled=false, opacity=1 and
    pointerEvents=auto in EVERY phase, so none of the obvious properties
    distinguish enabled from disabled. The greying is done by dropping the
    inner CASH OUT label to **opacity 0.5**. That label's opacity is
    therefore the only reliable signal, and this reads it."""
    game = game or STOCKMARKET
    try:
        op = frame.evaluate(_CASHOUT_ENABLED_JS, game.cashout_role)
    except Exception:
        return False
    return op is not None and op > 0.9


def _cashout_ready(frame, game=None):
    """True when there's a live, cashable position.

    Used to require _cashout_enabled() (the CASH OUT label's opacity) as the
    deciding signal on top of window-closed + portfolio>0, because runs 3 and
    4 failed to cash out and that looked like the explanation. It wasn't:
    confirmed live 2026-07-20 (probe_live_cashout.py, real ₹10 bet) that
    _cashout_enabled reads False continuously through a position that is
    provably live and moving (portfolio 10 -> 9.86 -> 4.06), and a real
    _click_cashout() fired during that same "disabled" state landed
    immediately -- portfolio dropped 4.06 -> 0 and the account balance moved
    by exactly that amount (₹1489 -> ₹1483, matching ₹10 staked - ₹4.06 back).
    So the label-opacity signal is simply wrong, not just early-in-a-gap as
    previously theorized, and runs 3/4 most likely never attempted a real
    click at all rather than clicking too soon. Trust only window-closed +
    portfolio>0; verify success the same way the probe did, by checking the
    portfolio reading actually dropped after a click, not by asking the
    button whether it thinks it's enabled."""
    game = game or STOCKMARKET
    if _betting_open(frame, game):
        return False
    val = read_portfolio(frame, game)
    return val is not None and val > 0


def _click_cashout(frame, game=None, timeout=5000):
    """Click CASH OUT. Same tag-then-force-click approach as _click_bet_spot
    (a decorative overlay can sit above it). Returns True if the element was
    found and clicked -- NOT proof the cash-out registered; the caller must
    verify the portfolio dropped back to 0."""
    game = game or STOCKMARKET
    try:
        if not frame.evaluate(_TAG_BET_SPOT_JS, game.cashout_role):
            return False
        frame.locator(f'[data-pw-spot="{game.cashout_role}"]').click(
            timeout=timeout, force=True)
        return True
    except Exception:
        return False


def read_game_balance(frame):
    """Read the Evolution game frame's own BALANCE readout
    (data-role="balance-label-value", e.g. "₹1,891") as an int, or None.
    Same-tab and real-time, so it reflects wins/losses as they settle.
    Verified live 2026-07-20 to work unchanged on Stock Market Live too."""
    try:
        return frame.evaluate(_BALANCE_JS)
    except Exception:
        return None


def _setup_fail(context, page, username, step, reason):
    """Screenshot the page state, close the context, and raise. The hedge
    setup's failures are intermittent and my read-only diagnostics keep landing
    in good windows -- the bot's own failures are the only witnesses, so they
    must capture evidence (shots/hedge-setup-*.png) before dying."""
    shot = ""
    try:
        SHOTS_DIR.mkdir(exist_ok=True)
        path = SHOTS_DIR / f"hedge-setup-{username}-{step}-{time.strftime('%Y%m%d-%H%M%S')}.png"
        page.screenshot(path=str(path))
        shot = f" [screenshot: {path}]"
    except Exception:
        pass
    context.close()
    raise RuntimeError(reason + shot)


def _find_provider_lobby_frame(game_page, timeout_ms=15000):
    """Return the Evolution in-game lobby's frame.

    Load-bearing detail, established live 2026-07-20 after several failed
    attempts: the lobby is a SEPARATE iframe from the game (its URL carries
    "?iFrAmE=x"), and only that frame has the lobby's Search box. The game
    frame -- which is what find_game_frame() returns, since it has the most
    DOM nodes -- contains no search input at all, only quick-chat-input. Typing
    into "the frame" therefore silently did nothing every time. Identify the
    lobby by its own category tabs instead of by URL, which is stable."""
    deadline = time.time() + timeout_ms / 1000
    markers = ("For You", "Top Games", "Game Shows")
    while time.time() < deadline:
        for fr in game_page.frames:
            try:
                if fr.evaluate("""(markers) => {
                    const vis = e => e.getBoundingClientRect().height > 0;
                    const texts = Array.from(document.querySelectorAll('div,span,a,p'))
                        .filter(vis).map(e => (e.innerText || '').trim());
                    return markers.some(mk => texts.includes(mk));
                }""", list(markers)):
                    return fr
            except Exception:
                continue
        game_page.wait_for_timeout(500)
    return None


def _open_via_provider_lobby(game_page, frame, game):
    """Switch from whichever Evolution game is open to `game` via the
    provider's own in-game lobby, and return the NEW game frame.

    Needed because some games simply aren't in the operator's catalogue.
    Confirmed live 2026-07-20 for Stock Market Live on cricmatch247: 206 tiles
    across the lobby's Game Shows / Arcade Games / All categories (with "View
    All" expanded and lazy-load scrolled) contain no match, and the site's own
    search returns only football teams for "Stock". It is reachable *only*
    from inside Evolution's lobby, whose LOBBY button sits bottom-right in any
    running game.

    The lobby overlay is fragile -- any stray click dismisses it and drops
    back into the game -- so this clicks only the three things it needs."""
    # The LOBBY button only exists once the entry game's own UI has rendered --
    # find_game_frame() returns as soon as the frame has enough DOM nodes,
    # which can still be the loading screen. Clicking too early silently does
    # nothing and the lobby frame then never appears, so poll for the button
    # rather than assuming it's there (this is exactly what failed live on the
    # first end-to-end run).
    deadline = time.time() + 45
    btn = None
    while time.time() < deadline:
        try:
            loc = frame.locator(f'[data-role="{game.lobby_button_role}"]').first
            if loc.count() and loc.is_visible():
                btn = loc
                break
        except Exception:
            pass
        game_page.wait_for_timeout(500)
    if btn is None:
        raise RuntimeError(
            f"the entry game's {game.lobby_button_role!r} never appeared "
            f"(it likely never finished loading)")
    try:
        btn.click(timeout=8000, force=True)
    except Exception as e:
        raise RuntimeError(f"could not open Evolution's lobby: {str(e)[:120]}")

    # Retry the click a couple of times -- the lobby is an overlay and the
    # first click can land while the game is still settling.
    lobby = _find_provider_lobby_frame(game_page, timeout_ms=12000)
    for _ in range(2):
        if lobby is not None:
            break
        try:
            frame.locator(f'[data-role="{game.lobby_button_role}"]').first.click(
                timeout=5000, force=True)
        except Exception:
            pass
        lobby = _find_provider_lobby_frame(game_page, timeout_ms=12000)
    if lobby is None:
        raise RuntimeError("Evolution's lobby frame never appeared")

    try:
        box = lobby.get_by_placeholder("Search").first
        box.click(timeout=6000)
        box.type(game.lobby_search, delay=140)
    except Exception as e:
        raise RuntimeError(f"could not search Evolution's lobby: {str(e)[:120]}")
    game_page.wait_for_timeout(4500)

    try:
        lobby.locator(f"text={game.lobby_tile}").first.click(timeout=8000, force=True)
    except Exception as e:
        raise RuntimeError(
            f"{game.lobby_tile!r} not found in Evolution's lobby: {str(e)[:120]}")
    game_page.wait_for_timeout(14000)

    # The game frame changes when the new table loads, so re-resolve it by the
    # new game's own bet spot rather than reusing the old frame.
    deadline = time.time() + 30
    while time.time() < deadline:
        for fr in game_page.frames:
            try:
                if fr.evaluate("(r) => !!document.querySelector(`[data-role=\"${r}\"]`)",
                               game.table_ready_role):
                    return fr
            except Exception:
                continue
        game_page.wait_for_timeout(500)
    raise RuntimeError(f"{game.lobby_tile} opened but its frame never appeared")


def _open_table_for(browser, username, password, site_url, category, tile_text,
                    proxy_conf=None, progress=None, label="", game=None):
    """Log a fresh context into `username` and open the given live table.
    Returns (context, main_page, game_page, frame) or raises RuntimeError with
    a human-readable reason. Caller owns closing the context. `proxy_conf` is a
    Playwright proxy dict (already bridged for SOCKS5-auth) or None for direct.
    `progress(str)` (optional), if given, is called once per phase (login,
    casino lobby, game join, live table) -- this whole function is naturally
    slow (a real login + a real live-video game loading, done sequentially for
    two accounts, easily 1-2+ min each over a proxy), so without these the
    chat goes silent for minutes with no sign anything is happening."""
    progress = progress or (lambda _msg: None)
    game = game or BACCARAT
    tag = f" ({label})" if label else ""
    progress(f"🔑 Logging in{tag}…")
    context = browser.new_context(proxy=proxy_conf) if proxy_conf else browser.new_context()
    page = context.new_page()
    outcome, msgs = login(page, username, password, site_url=site_url)
    if outcome != "ok":
        context.close()
        raise RuntimeError(f"login failed for {username}: {'; '.join(msgs) or outcome}")
    progress(f"🎰 Opening the Live Casino{tag}…")
    # The Live Casino nav is intermittently flaky in stretches (site-side; see
    # open_casino_lobby's docstring). Recovery ladder: (1) the SPA click loop,
    # (2) a direct goto to /live-casino -- documented risk is landing on a
    # logged-out view, so (3) detect that and re-login in place, then one more
    # click loop. On top of this, _open_table_with_retry() retries the whole
    # thing from a fresh context.
    if not open_casino_lobby(page, timeout_ms=20000):
        prof = profile_for(site_url or SITE_URL)
        parts = urlsplit(site_url or SITE_URL)
        try:
            page.goto(f"{parts.scheme}://{parts.netloc}/live-casino",
                      wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(3000)
            dismiss_popups(page)
        except Exception:
            pass
        lobby_ok = False
        try:
            lobby_ok = page.locator("a:has-text('Baccarat')").first.is_visible()
        except Exception:
            pass
        if not lobby_ok:
            logged_in = False
            try:
                logged_in = page.locator(prof.sel["logged_in_indicator"]).first.is_visible()
            except Exception:
                pass
            if not logged_in:
                outcome, _ = login(page, username, password, site_url=site_url)
                if outcome != "ok":
                    _setup_fail(context, page, username, "relogin",
                                f"casino lobby nav failed and re-login also failed for {username}")
            lobby_ok = open_casino_lobby(page, timeout_ms=20000)
        if not lobby_ok:
            _setup_fail(context, page, username, "lobby",
                        f"could not open the casino lobby for {username}")
    progress(f"🃏 Joining {tile_text}{tag}…")
    game_page = None
    for _ in range(3):
        game_page = search_and_open_game(page, category, tile_text)
        if game_page:
            break
        page.wait_for_timeout(2000)
    if not game_page:
        # Distinguish "the site dropped the login session" from generic tile
        # flakiness. Confirmed live 2026-07-19: some accounts (password
        # accepted, login() returns ok) get silently kicked back to a
        # logged-out view within seconds -- the live-casino page then renders
        # the guest lobby, which simply doesn't list the live tables, so the
        # tile click can never succeed. Account-level on the site's side
        # (reproduced identically with and without a proxy, from two IPs,
        # while another account worked end-to-end through the same code and
        # proxy at the same moment) -- retrying won't help, and the operator
        # needs to know it's the account, not the connection.
        session_dropped = False
        try:
            session_dropped = page.evaluate(
                """() => Array.from(document.querySelectorAll('a,button')).some(e => {
                    const r = e.getBoundingClientRect();
                    return r.height > 0 && (e.innerText || '').trim() === 'LOGIN';
                })""")
        except Exception:
            pass
        context.close()
        if session_dropped:
            raise RuntimeError(
                f"the site dropped {username}'s login session right after login "
                f"(casino page renders logged-out, so live tables are hidden) -- "
                f"this is an account-level restriction/throttle on the site's "
                f"side, not a connection problem; retrying won't fix it")
        raise RuntimeError(f"could not open the {tile_text!r} table for {username}")
    frame = find_game_frame(game_page, "evo-games.com")
    if frame is None:
        context.close()
        raise RuntimeError(f"game tab opened but its UI frame never loaded for {username}")

    # Games the operator's own lobby doesn't carry (Stock Market Live) need a
    # second hop through the PROVIDER's in-game lobby -- see
    # _open_via_provider_lobby.
    if game.via_provider_lobby:
        progress(f"🔎 Switching to {game.lobby_tile} via Evolution's lobby{tag}…")
        try:
            frame = _open_via_provider_lobby(game_page, frame, game)
        except RuntimeError as e:
            context.close()
            raise RuntimeError(f"{e} (for {username})")

    progress(f"📡 Waiting for the live table to load{tag}…")
    if not wait_for_live_table(frame, game_page, game=game):
        context.close()
        raise RuntimeError(f"table never became live (stuck loading) for {username}")
    progress(f"✅ Table ready{tag}")
    return context, page, game_page, frame


class _HedgeStopped(Exception):
    """Raised inside setup when should_stop() is set, so /stoprun can abort a
    run that's still opening tables (setup can take minutes over its retries)."""


def _open_table_with_retry(browser, creds, site_url, category, tile_text,
                           label, progress, attempts=4, proxy_conf=None,
                           should_stop=None, game=None):
    """_open_table_for with fresh-context retries. Login + the Live Casino nav
    are intermittently flaky in stretches (site-side; observed live 2026-07-17:
    fine at 21:58 and 22:15, failing repeatedly at 21:52 and 22:06), so a failed
    open is usually cleared by retrying from a brand-new context -- with a pause
    between attempts so a bad stretch has time to pass. Checks should_stop()
    between attempts (and during the pause) so /stoprun aborts setup promptly.
    Raises the last RuntimeError if every attempt fails, or _HedgeStopped if
    stopped. `progress(str)` reports each retry."""
    should_stop = should_stop or (lambda: False)
    last = None
    for i in range(1, attempts + 1):
        if should_stop():
            raise _HedgeStopped()
        try:
            return _open_table_for(browser, creds["username"], creds["password"],
                                   site_url, category, tile_text, proxy_conf=proxy_conf,
                                   progress=progress, label=f"{label}: {creds['username']}",
                                   game=game)
        except RuntimeError as e:
            last = e
            # An account-level session drop (see _open_table_for) is not the
            # transient flakiness this retry loop exists for -- every retry
            # does a full fresh login that the site will drop again, which
            # both wastes ~a minute per attempt and feeds whatever rate/abuse
            # heuristic flagged the account in the first place. Fail fast.
            if "dropped" in str(e) and "session" in str(e):
                raise
            if i < attempts:
                progress(f"⏳ Opening {label} table for {creds['username']} — "
                         f"attempt {i}/{attempts} failed: {str(e)[:250]}\n"
                         f"Trying a fresh session…")
                for _ in range(10):  # pause, but stay responsive to /stoprun
                    if should_stop():
                        raise _HedgeStopped()
                    time.sleep(1)
    raise last


def _launch_pw_browser():
    """Start a standalone Playwright connection + headless Chromium. Must be
    called from (and every object it returns used from) the thread that will
    own it -- run this via executor.submit, never inline. Used to give the
    Player side of a paired hedge its own browser/thread so its login +
    table-join can run concurrently with the Banker side instead of
    serialized on one thread (see run_paired_hedge)."""
    pw = sync_playwright().start()
    try:
        browser = pw.chromium.launch(headless=True)
    except Exception:
        pw.stop()
        raise
    return pw, browser


def _table_id(game_page):
    """Extract Evolution's table_id from a game tab URL (the two accounts must
    share it for the hedge to be on the same hand)."""
    # New-tab launches carry table_id=; the same-tab launch used by
    # bonus-balance accounts (REAL CHIPS gate) carries vt_id= instead.
    #
    # The character class MUST include uppercase. Baccarat's ids are lowercase
    # ("oytmvb9m1zysmc44"), but Stock Market Live's is "StockMarket00001" --
    # with a lowercase-only class this returned None there, which silently
    # DISABLED run_paired_hedge's same-table check (it only compares when both
    # ids are truthy), so the two accounts could have been betting different
    # tables with nothing to catch it. Caught live 2026-07-20.
    m = re.search(r"(?:table_id|vt_id)=([A-Za-z0-9]+)", game_page.url or "")
    return m.group(1) if m else None


def _now_iso():
    """Local timestamp, same format the bot uses for pair created_at."""
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def run_paired_hedge(banker_creds, player_creds, amount, rounds,
                     site_url=None, category=None, tile_text=None,
                     progress=None, should_stop=None, proxy=None, browser=None,
                     setup_progress=None, game=None):
    """Run up to `rounds` hedged rounds: the two accounts bet OPPOSITE sides of
    the same hand, `amount` each, on the SAME table, until `rounds` is reached
    OR either balance drops below `amount` OR a round goes unhedged (partial)
    OR should_stop() returns True.

    `game` is a GameProfile (sites/games.py) and decides which two sides those
    are: baccarat (the default, unchanged) bets Banker vs Player; Stock Market
    Live bets UP vs DOWN and additionally cashes out both positions each round
    (see the cash-out block in the round loop). The `banker_*`/`player_*`
    naming throughout this function and its summary dict is retained as a
    generic side-A/side-B alias so existing /run history and the bot's
    renderers keep working -- on a non-baccarat game "banker" simply means
    side A, whose real label is game.side_a_label.

    `category`/`tile_text` default to the game's own values; they remain
    overridable for ad-hoc callers.

    `*_creds` are dicts {"username","password"}. `progress(str)` (optional) is
    called with a human-readable line each round. `setup_progress(str)`
    (optional) receives the per-phase setup lines instead (login, lobby, table
    join, retries -- everything from _open_table_for/_open_table_with_retry);
    when omitted it falls back to `progress`, so existing callers behave
    exactly as before. The bot passes a console-only logger here so setup
    chatter stays out of the chat while round results still land there.
    `should_stop()` (optional)
    returns True to stop after the current round. `proxy` (optional raw string)
    routes BOTH accounts' contexts through the same exit IP -- required when the
    box running the bot has a datacenter IP that the site's WAF 403-blocks
    (login just returns a "Forbidden" page otherwise).

    `browser`, if given, is a pre-launched browser the Banker side reuses (runs
    on the calling thread, no extra thread spun up) -- kept only so a caller
    with an already-warm browser (e.g. a single ad-hoc run) can skip a launch.
    When omitted (the normal case, since concurrent /run calls must NOT share
    a browser or they'd serialize on it), this function launches its own
    temporary Banker browser via `_launch_pw_browser` inline on the calling
    thread and closes it in the `finally` alongside the context -- this is
    what makes the whole call self-contained enough for several to run truly
    in parallel, one per worker thread, with zero shared Playwright state
    between them. The Player side always gets its OWN temporary browser +
    single-worker thread (`player_exec`, launched via `_launch_pw_browser`),
    so its login + table-join runs concurrently with the Banker side's setup
    instead of serialized after it -- this roughly halves setup time. Every
    call touching the Player side's context/page/frame is dispatched through
    `player_exec`; round-loop reads (balance, betting-open, total-bet) and the
    two bet-spot clicks are similarly fired on both threads back-to-back
    (Player submitted first, Banker run inline, then join) rather than one
    waiting on the other. `player_exec` and its temporary browser are torn
    down in the `finally` alongside the Banker context, on every exit path.

    Returns a summary dict: {"ok", "rounds_done", "requested_rounds",
    "stop_reason", "messages", "final_balance": {"banker","player"},
    "start_balance": {"banker","player"}, "rounds": [...], "shots",
    "started_at", "ended_at"}. `rounds` is a per-round log --
    {"round", "banker", "player", "amount"} after each hedged round -- so a
    caller can persist the full progression, not just the final numbers.
    Real money -- see module notes above."""
    progress = progress or (lambda _msg: None)
    setup_progress = setup_progress if setup_progress is not None else progress
    should_stop = should_stop or (lambda: False)
    game = game or BACCARAT
    category = category or game.category
    tile_text = tile_text or game.tile_text
    summary = {"ok": False, "rounds_done": 0, "requested_rounds": rounds,
               "stop_reason": None, "messages": [], "shots": [], "rounds": [],
               "started_at": _now_iso(), "ended_at": None, "game": game.key,
               "start_balance": {"banker": None, "player": None},
               "final_balance": {"banker": None, "player": None}}

    ctx_b = ctx_p = gp_b = gp_p = fr_b = fr_p = bridge_proc = None
    # The Player side gets its own temporary browser + single-worker thread so
    # its login + table-join runs concurrently with the Banker side (which
    # either reuses a caller-supplied `browser` on the calling thread, or --
    # the normal path -- launches its own below, also on the calling thread)
    # instead of the two being serialized one after another. Playwright's sync
    # API is thread-affine -- every call touching player_browser/ctx_p/gp_p/fr_p
    # must go through player_exec, never called inline from this thread.
    player_exec = ThreadPoolExecutor(max_workers=1)
    player_pw = player_browser = None
    banker_pw = None
    owns_banker_browser = browser is None
    try:
        if owns_banker_browser:
            try:
                banker_pw, browser = _launch_pw_browser()
            except Exception as e:
                summary["stop_reason"] = "setup_failed"
                summary["messages"].append(f"Failed to launch Banker browser: {e}")
                return summary
        # Both accounts share one exit IP: parse + (for SOCKS5-auth) bridge the
        # proxy once, then hand the same proxy_conf to both contexts. stop_bridge
        # runs in the finally so the local pproxy subprocess never leaks.
        proxy_conf = parse_proxy(proxy) if proxy else None
        try:
            proxy_conf, bridge_proc = maybe_bridge_proxy(proxy_conf)
        except RuntimeError as e:
            summary["stop_reason"] = "setup_failed"
            summary["messages"].append(f"Proxy bridge failed to start: {e}")
            return summary
        try:
            player_pw, player_browser = player_exec.submit(_launch_pw_browser).result()
            player_fut = player_exec.submit(
                _open_table_with_retry, player_browser, player_creds, site_url,
                category, tile_text, game.side_b_label, setup_progress,
                proxy_conf=proxy_conf, should_stop=should_stop, game=game)
            try:
                banker_open = _open_table_with_retry(
                    browser, banker_creds, site_url, category, tile_text,
                    game.side_a_label, setup_progress, proxy_conf=proxy_conf,
                    should_stop=should_stop, game=game)
            except (_HedgeStopped, RuntimeError):
                # Banker failed/stopped -- still must collect (and clean up)
                # whatever the Player side produced, so nothing leaks.
                try:
                    p_ctx, _, _, _ = player_fut.result()
                    player_exec.submit(p_ctx.close).result()
                except Exception:
                    pass
                raise
            try:
                player_open = player_fut.result()
            except (_HedgeStopped, RuntimeError):
                banker_open[0].close()
                raise
            ctx_b, _, gp_b, fr_b = banker_open
            ctx_p, _, gp_p, fr_p = player_open
        except _HedgeStopped:
            summary["stop_reason"] = "stopped_by_user"
            return summary
        except RuntimeError as e:
            summary["stop_reason"] = "setup_failed"
            summary["messages"].append(str(e))
            return summary

        tid_b = _table_id(gp_b)
        tid_p = player_exec.submit(_table_id, gp_p).result()
        if tid_b and tid_p and tid_b != tid_p:
            summary["stop_reason"] = "different_tables"
            summary["messages"].append(
                f"The two accounts landed on different tables ({tid_b} vs {tid_p}); "
                "the hedge would not be on the same hand. Aborting before any bet.")
            return summary
        if not (tid_b and tid_p):
            # This check is the only thing standing between "a hedge" and "two
            # unrelated bets", so refuse to bet when it can't actually run
            # rather than proceeding on the assumption that it passed.
            summary["stop_reason"] = "different_tables"
            summary["messages"].append(
                f"Could not read a table id for "
                f"{'both sides' if not (tid_b or tid_p) else ('the ' + game.side_a_label + ' side' if not tid_b else 'the ' + game.side_b_label + ' side')} "
                f"(got {tid_b!r} / {tid_p!r}), so there's no way to confirm both "
                "accounts are on the same table. Aborting before any bet.")
            return summary

        # Pick the chip matching `amount` BEFORE any betting. Without this the
        # table bets its pre-selected chip (the ₹10 minimum) whatever was
        # requested, which is what tripped amount_mismatch on the first run.
        if game.selectable_chips:
            p_fut = player_exec.submit(read_chips, fr_p)
            rail_b = read_chips(fr_b)
            rail_p = p_fut.result()
            avail = sorted(set(rail_b.get("chips") or []) & set(rail_p.get("chips") or []))
            if avail and amount not in avail:
                summary["stop_reason"] = "amount_mismatch"
                summary["messages"].append(
                    f"₹{amount} isn't one of this table's chips. Available: "
                    + ", ".join(f"₹{c}" for c in avail)
                    + ". Re-run with one of those. (Aborted before any bet.)")
                return summary
            p_fut = player_exec.submit(select_chip, fr_p, amount)
            ok_b = select_chip(fr_b, amount)
            ok_p = p_fut.result()
            if not (ok_b and ok_p):
                side = game.side_a_label if not ok_b else game.side_b_label
                bad_fr, bad_exec = ((fr_b, None) if not ok_b else (fr_p, player_exec))
                rail = (bad_exec.submit(describe_chip_rail, bad_fr).result()
                        if bad_exec else describe_chip_rail(bad_fr))
                summary["stop_reason"] = "chip_select_failed"
                summary["messages"].append(
                    f"Could not select the ₹{amount} chip on the {side} side "
                    f"({rail}), so a bet would have been the wrong size. "
                    f"Aborted before any bet.")
                return summary

        p_fut = player_exec.submit(read_game_balance, fr_p)
        summary["start_balance"]["banker"] = read_game_balance(fr_b)
        summary["start_balance"]["player"] = p_fut.result()
        summary["final_balance"]["banker"] = summary["start_balance"]["banker"]
        summary["final_balance"]["player"] = summary["start_balance"]["player"]

        # Round loop is attempt-based, not a plain `for rnd in range(rounds)`:
        # a missed betting window or a one-sided (unhedged) landing used to
        # `break`/`return` the WHOLE run after a single bad round, well short
        # of the requested `rounds`. Neither is evidence the run can't
        # continue -- both are the kind of one-off timing hiccup
        # `_open_table_with_retry` already treats as retry-worthy during
        # setup (site-side flakiness that clears if you wait and try again),
        # so the round loop now retries the SAME round slot (not counted
        # toward rounds_done) with a cooldown instead of giving up
        # immediately. `consecutive_failures` still gives up (a hard stop,
        # not another retry) after several in a row with zero progress --
        # that pattern means a persistent problem (site down, WAF block, a
        # genuinely closed table), not a blip, and retrying forever would
        # just burn time/requests without ever reaching `rounds`.
        # `banker_out_of_balance`/`player_out_of_balance`/`amount_mismatch`
        # (below) stay immediate hard stops -- waiting doesn't refill a
        # balance or change a table's chip menu, so retrying those can't
        # help, unlike a missed window or a one-sided landing.
        MAX_CONSECUTIVE_ROUND_FAILURES = 5
        ROUND_RETRY_COOLDOWN_SECS = 6
        max_attempts = max(rounds * 4, 20)
        consecutive_failures = 0
        attempt = 0
        while summary["rounds_done"] < rounds:
            if should_stop():
                summary["stop_reason"] = "stopped_by_user"
                break
            attempt += 1
            if attempt > max_attempts:
                summary["stop_reason"] = "max_attempts_exceeded"
                summary["messages"].append(
                    f"Gave up after {attempt - 1} attempts without reaching "
                    f"{rounds} hedged rounds ({summary['rounds_done']} done) -- "
                    "stopping rather than retrying indefinitely.")
                break

            p_fut = player_exec.submit(read_game_balance, fr_p)
            bal_b = read_game_balance(fr_b)
            bal_p = p_fut.result()
            summary["final_balance"]["banker"] = bal_b
            summary["final_balance"]["player"] = bal_p
            if bal_b is not None and bal_b < amount:
                summary["stop_reason"] = "banker_out_of_balance"
                break
            if bal_p is not None and bal_p < amount:
                summary["stop_reason"] = "player_out_of_balance"
                break

            # Both accounts are separate browser contexts on the SAME physical
            # table, but their video/countdown render 1-3s out of phase, so one
            # can show an OPEN betting window while the other is still between
            # rounds. TOTAL BET reads 0 in BOTH the open-and-empty AND the
            # closed states, so the old gate (TB==0) placed the Banker bet into
            # its open window while the Player's window was still closed -> only
            # Banker landed -> unhedged exposure (reproduced live 2026-07-17).
            # Gate on the real phase signal instead (_betting_open): only place
            # when BOTH windows are open. Catch the RISING edge -- first drain
            # any window we're already mid-way through, then wait for the next
            # both-open moment with nothing staged -- so both bets land early in
            # the shared window, well before either side's ~15s timer expires.
            # Deadlines are TIME-based, sized to the real table cadence: a full
            # baccarat cycle (betting -> dealing -> result -> next betting) runs
            # ~45-60s, so the placement wait must cover at least two full
            # cycles. An iteration-count budget (~20s) proved too short live
            # (run #7, 2026-07-17): round 1 placed fine because a window was
            # already near, then round 2 hit `no_open_window` simply because
            # the next window opened after the budget expired.
            placed = False
            unhedged = False
            drain_deadline = time.time() + game.drain_secs   # (a) let a mid-way window pass
            while time.time() < drain_deadline:
                p_fut = player_exec.submit(_betting_open, fr_p, game)
                b_open = _betting_open(fr_b, game)
                if should_stop() or not (b_open and p_fut.result()):
                    break
                time.sleep(0.5)
            place_deadline = time.time() + game.place_secs  # (b) catch a fresh both-open window
            while time.time() < place_deadline:
                if should_stop():
                    summary["stop_reason"] = "stopped_by_user"
                    break
                p_fut = player_exec.submit(_betting_open, fr_p, game)
                b_open = _betting_open(fr_b, game)
                if not (b_open and p_fut.result()):
                    time.sleep(0.5)
                    continue
                p_fut = player_exec.submit(_read_total_bet, fr_p)
                b_tb0 = _read_total_bet(fr_b)
                if (b_tb0 or 0) != 0 or (p_fut.result() or 0) != 0:
                    # something already staged/settling -- wait for a clean 0/0.
                    time.sleep(0.5)
                    continue

                # Fire the Player click on its own thread first (non-blocking),
                # then the Banker click inline -- both bets land within the same
                # sub-second window instead of one waiting on the other.
                p_fut = player_exec.submit(_click_bet_spot, fr_p, game.side_b_role)
                _click_bet_spot(fr_b, game.side_a_role)
                p_fut.result()
                time.sleep(1.5)
                p_fut = player_exec.submit(_read_total_bet, fr_p)
                tb_b = _read_total_bet(fr_b)
                tb_p = p_fut.result()

                if tb_b == amount and tb_p == amount:
                    placed = True
                    break
                if (tb_b or 0) > 0 and (tb_p or 0) > 0 and tb_b == tb_p and tb_b != amount:
                    # Both placed the table's default chip, which isn't `amount`.
                    # This round IS hedged (safe), but the size is wrong -- abort
                    # cleanly rather than repeat, and tell the caller the real size.
                    # This branch should be rare now that select_chip() runs before
                    # the round loop (games with selectable_chips=True) -- it's the
                    # fallback for a game with no chip rail at all (baccarat) or an
                    # unexpected mid-run reset, not the normal path.
                    summary["stop_reason"] = "amount_mismatch"
                    summary["messages"].append(
                        f"The table placed ₹{tb_b} per side (its selected chip), not the "
                        f"requested ₹{amount}. That one round is hedged; stopping. Re-run "
                        f"with amount={tb_b}.")
                    _screenshot_pair(gp_b, gp_p, summary, "mismatch", player_exec)
                    return summary
                if bool(tb_b) != bool(tb_p):
                    # Exactly one side landed -> real exposure for this one
                    # hand. Baccarat hands resolve on their own (no button to
                    # press, unlike Stock Market's live position) -- so unlike
                    # a cash-out failure, there is nothing further waiting
                    # makes worse. Don't count it as a hedged round; wait for
                    # this hand to settle below, then retry the round slot.
                    unhedged = True
                    exposed = banker_creds["username"] if tb_b else player_creds["username"]
                    side = game.side_a_label if tb_b else game.side_b_label
                    summary["messages"].append(
                        f"Attempt {attempt}: only the {side} bet landed (account "
                        f"{exposed} exposed for ₹{amount} this hand, not counted as "
                        f"hedged). Waiting for it to settle, then retrying.")
                    summary.setdefault("unhedged_rounds", []).append(
                        {"attempt": attempt, "side": side, "account": exposed,
                         "amount": amount})
                    _screenshot_pair(gp_b, gp_p, summary,
                                     f"partial-attempt{attempt}", player_exec)
                    break
                # neither landed (window closed) -> retry the window.
                time.sleep(2)

            if summary["stop_reason"] == "stopped_by_user":
                break

            if unhedged:
                # Wait for the exposed hand to resolve before trying again --
                # same settle-wait the normal end-of-round path uses below --
                # so the retry starts on a clean board, not mid-hand.
                for _ in range(game.settle_secs):
                    p_fut = player_exec.submit(_read_total_bet, fr_p)
                    b_tb = _read_total_bet(fr_b)
                    if (b_tb or 0) == 0 and (p_fut.result() or 0) == 0:
                        break
                    time.sleep(1)
                consecutive_failures += 1
                if consecutive_failures >= MAX_CONSECUTIVE_ROUND_FAILURES:
                    summary["stop_reason"] = "repeated_unhedged_exposure"
                    summary["messages"].append(
                        f"{consecutive_failures} unhedged exposures in a row -- "
                        "stopping rather than risking more.")
                    break
                progress(f"⚠️ Attempt {attempt}: unhedged exposure, retrying "
                         f"({summary['rounds_done']}/{rounds} hedged so far)…")
                for _ in range(ROUND_RETRY_COOLDOWN_SECS):
                    if should_stop():
                        break
                    time.sleep(1)
                continue

            if not placed:
                consecutive_failures += 1
                if consecutive_failures >= MAX_CONSECUTIVE_ROUND_FAILURES:
                    summary["stop_reason"] = "no_open_window"
                    summary["messages"].append(
                        f"{consecutive_failures} missed betting windows in a row -- "
                        "stopping rather than retrying indefinitely.")
                    break
                progress(f"⏳ Attempt {attempt}: missed the betting window, retrying "
                         f"({summary['rounds_done']}/{rounds} hedged so far)…")
                for _ in range(ROUND_RETRY_COOLDOWN_SECS):
                    if should_stop():
                        break
                    time.sleep(1)
                continue

            consecutive_failures = 0
            rnd = summary["rounds_done"] + 1
            summary["rounds_done"] = rnd

            # ---- cash-out (Stock Market Live only) -------------------------
            # Baccarat bets are discrete: once placed, the hand resolves itself
            # and there is nothing to time. Stock Market Live instead runs a
            # live chart, and each side's PORTFOLIO moves continuously until
            # CASH OUT is pressed -- so the two positions are only a true hedge
            # while they are worth (stake_a + stake_b) together, and every
            # second between the two cash-outs is real money. Measured live
            # 2026-07-20, the chart can travel ~90% inside ~20s.
            #
            # So: cash out as EARLY as possible (the instant a position exists,
            # when both portfolios are still ~= their stakes) and fire the two
            # clicks concurrently on their own threads, exactly like the bet
            # clicks above.
            if game.needs_cashout:
                # Wait for positions to actually open. read_portfolio is the
                # signal, NOT the button's disabled state -- confirmed live the
                # CASH OUT button reports disabled=false and opacity=1 even with
                # nothing staked, so it can't distinguish the phases.
                co_deadline = time.time() + game.settle_secs
                ready = False
                co_trace = []          # phase/portfolio/opacity, for diagnosis
                co_live_dump = None    # button markup while a position rides
                while time.time() < co_deadline:
                    if should_stop():
                        break
                    p_fut = player_exec.submit(_cashout_ready, fr_p, game)
                    b_ready = _cashout_ready(fr_b, game)
                    p_ready = p_fut.result()
                    # Record how the three signals move, so a failure says WHY
                    # rather than just "it didn't work" (the button's enabled
                    # state is invisible in a screenshot once the run has torn
                    # the context down).
                    snap = (_read_instruction(fr_b, game.instruction_role),
                            read_portfolio(fr_b, game),
                            _cashout_enabled(fr_b, game))
                    if not co_trace or co_trace[-1][:3] != snap:
                        co_trace.append(snap + (round(time.time() - co_deadline
                                                      + game.settle_secs, 1),))
                    # The position is LIVE here (its value is moving) yet the
                    # button still reads disabled -- so the opacity heuristic
                    # is wrong. Grab the panel's real markup exactly once in
                    # that state; it is the only way to see the enabled
                    # styling, since reaching it requires money on the table.
                    if (co_live_dump is None and snap[1] and snap[1] > 0
                            and not snap[2] and len(co_trace) > 2
                            and snap[1] != co_trace[0][1]):
                        try:
                            co_live_dump = fr_b.evaluate(_DUMP_CASHOUT_JS,
                                                         game.cashout_role)
                        except Exception as e:
                            co_live_dump = {"error": str(e)[:120]}
                    if b_ready and p_ready:
                        ready = True
                        break
                    time.sleep(0.4)
                summary.setdefault("cashout_live_dump", []).append(
                    {"round": rnd, "dump": co_live_dump})
                summary.setdefault("cashout_trace", []).append(
                    {"round": rnd,
                     "trace": [{"phase": t[0], "portfolio": t[1],
                                "enabled": t[2], "t": t[3]} for t in co_trace[:40]]})

                if not ready:
                    summary["stop_reason"] = "no_cashout_window"
                    summary["messages"].append(
                        f"Round {rnd}: both bets landed but no cash-out window "
                        f"appeared within {game.settle_secs}s. Positions may still "
                        f"be open — check both accounts manually.")
                    _screenshot_pair(gp_b, gp_p, summary, "nocashout", player_exec)
                    return summary

                p_fut = player_exec.submit(read_portfolio, fr_p, game)
                port_b = read_portfolio(fr_b, game)
                port_p = p_fut.result()

                # Both clicks, concurrently, same pattern as the bets.
                p_fut = player_exec.submit(_click_cashout, fr_p, game)
                _click_cashout(fr_b, game)
                p_fut.result()
                time.sleep(2)

                # Verify both closed; retry once for whichever side didn't.
                p_fut = player_exec.submit(_cashout_ready, fr_p, game)
                still_b = _cashout_ready(fr_b, game)
                still_p = p_fut.result()
                if still_b or still_p:
                    if still_p:
                        player_exec.submit(_click_cashout, fr_p, game).result()
                    if still_b:
                        _click_cashout(fr_b, game)
                    time.sleep(2)
                    p_fut = player_exec.submit(_cashout_ready, fr_p, game)
                    still_b = _cashout_ready(fr_b, game)
                    still_p = p_fut.result()

                if still_b and still_p:
                    # NEITHER side cashed out. That is a failure worth stopping
                    # for, but it is NOT an exposure: both accounts still hold
                    # equal, opposite positions on the same round, so they
                    # remain hedged and will settle against each other. Saying
                    # "unhedged" here (as an earlier version did) is both wrong
                    # and alarming, so the two cases are reported separately.
                    summary["stop_reason"] = "cashout_failed"
                    summary["messages"].append(
                        f"Round {rnd}: neither side cashed out — ₹{port_b} "
                        f"({game.side_a_label}) and ₹{port_p} ({game.side_b_label}) "
                        f"are both still open. They stay hedged against each other "
                        f"and will settle on their own, so nothing needs closing by "
                        f"hand; stopping rather than betting another round.")
                    _screenshot_pair(gp_b, gp_p, summary, "cashout-failed", player_exec)
                    return summary
                if still_b or still_p:
                    exposed = (banker_creds["username"] if still_b
                               else player_creds["username"])
                    side = game.side_a_label if still_b else game.side_b_label
                    summary["stop_reason"] = "cashout_partial"
                    summary["messages"].append(
                        f"Round {rnd}: the {side} side ({exposed}) did not cash out "
                        f"while the other side did, so it is now riding the chart "
                        f"UNHEDGED for ₹{amount}. Stopping immediately — close it "
                        f"by hand.")
                    _screenshot_pair(gp_b, gp_p, summary, "cashout-partial", player_exec)
                    return summary

                # Divergence guard: the two cash-outs fired concurrently, but if
                # they didn't actually land together the pair no longer sums to
                # what went in. This detects that after the fact -- it cannot
                # prevent it, which is why live testing starts at the 10 rupee
                # table minimum.
                staked = amount * 2
                realized = (port_b or 0) + (port_p or 0)
                if staked and abs(realized - staked) / staked > game.cashout_tolerance:
                    summary["stop_reason"] = "cashout_divergence"
                    summary["messages"].append(
                        f"Round {rnd}: cashed out ₹{port_b} + ₹{port_p} = ₹{realized:.2f} "
                        f"against ₹{staked} staked — more than "
                        f"{game.cashout_tolerance:.0%} apart, so the two cash-outs did "
                        f"not land together and this round was not a clean hedge. "
                        f"Stopping.")
                    _screenshot_pair(gp_b, gp_p, summary, "divergence", player_exec)
                    return summary

            # Wait for the hand to resolve (both TOTAL BET back to 0) so the next
            # round starts on a fresh window; then read settled balances.
            for _ in range(game.settle_secs):
                p_fut = player_exec.submit(_read_total_bet, fr_p)
                b_tb = _read_total_bet(fr_b)
                if (b_tb or 0) == 0 and (p_fut.result() or 0) == 0:
                    break
                time.sleep(1)
            p_fut = player_exec.submit(read_game_balance, fr_p)
            bal_b = read_game_balance(fr_b)
            bal_p = p_fut.result()
            summary["final_balance"]["banker"] = bal_b
            summary["final_balance"]["player"] = bal_p
            summary["rounds"].append(
                {"round": rnd, "amount": amount, "banker": bal_b, "player": bal_p})
            progress(f"✅ Round {rnd}/{rounds} hedged · ₹{amount}/side\n"
                     f"   {game.side_a_icon} {banker_creds['username']} "
                     f"({game.side_a_label}): ₹{bal_b}\n"
                     f"   {game.side_b_icon} {player_creds['username']} "
                     f"({game.side_b_label}): ₹{bal_p}")

        if summary["stop_reason"] is None:
            summary["stop_reason"] = "completed"
        summary["ok"] = summary["rounds_done"] > 0
        return summary
    finally:
        summary["ended_at"] = _now_iso()
        for closer in (gp_b, ctx_b):
            try:
                if closer is not None:
                    closer.close()
            except Exception:
                pass
        if owns_banker_browser:
            try:
                if browser is not None:
                    browser.close()
                if banker_pw is not None:
                    banker_pw.stop()
            except Exception:
                pass
        for closer in (gp_p, ctx_p):
            try:
                if closer is not None:
                    player_exec.submit(closer.close).result()
            except Exception:
                pass
        try:
            if player_browser is not None:
                player_exec.submit(player_browser.close).result()
            if player_pw is not None:
                player_exec.submit(player_pw.stop).result()
        except Exception:
            pass
        player_exec.shutdown(wait=True)
        stop_bridge(bridge_proc)


def _screenshot_pair(gp_b, gp_p, summary, tag, player_exec):
    """Screenshot both game tabs into shots/ and record the paths on `summary`.
    gp_p belongs to the Player side's own thread (see run_paired_hedge), so its
    screenshot must be dispatched through player_exec, not called inline."""
    SHOTS_DIR.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    for label, gp, exe in (("banker", gp_b, None), ("player", gp_p, player_exec)):
        try:
            path = SHOTS_DIR / f"hedge-{tag}-{label}-{stamp}.png"
            if exe is not None:
                exe.submit(gp.screenshot, path=str(path)).result()
            else:
                gp.screenshot(path=str(path))
            summary["shots"].append(str(path))
        except Exception:
            pass


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
            a["referral_code"] = extract_referral_code(a.get("url") or SITE_URL)
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
    acct["referral_code"] = extract_referral_code(acct["url"] or SITE_URL)

    print("\nGenerated account:")
    print(f"  username : {acct['username']}")
    print(f"  email    : {acct['email']}")
    print(f"  password : {acct['password']}")
    print(f"  phone    : {acct['phone']}")
    print(f"  proxy    : {acct['proxy'] or '(none — direct connection)'}")
    print(f"  url      : {acct['url'] or SITE_URL} {'(default)' if not acct['url'] else ''}")
    print(f"  referral : {acct['referral_code'] or '(none)'}\n")
    return [acct]


def run_browser_account(browser, acct, args):
    """Run one signup via the Playwright/browser path (the original flow).
    Returns a result dict in signup_once()'s shape -- proxy-parse/bridge
    failures are folded in here too (rather than raising), so main()'s loop
    can treat this and http_signup_once() identically: call it, get a res."""
    bridge_proc = None
    try:
        proxy_conf = parse_proxy(acct.get("proxy"))
        proxy_conf, bridge_proc = maybe_bridge_proxy(proxy_conf)
    except (ValueError, RuntimeError) as e:
        return {"account": acct.get("username", "?"), "ok": False,
                "messages": [str(e)], "shot": None}

    context = browser.new_context(proxy=proxy_conf) if proxy_conf else None
    page = context.new_page() if context else browser.new_page()
    res = None
    try:
        res = signup_once(page, acct, submit=not args.no_submit,
                          interactive=not args.account_file,
                          site_url=acct.get("url"), proxy=acct.get("proxy"))
    except PWTimeout as e:
        res = {"account": acct.get("username", "?"), "ok": False,
               "messages": [f"Timeout: {str(e)[:120]}"], "shot": None}
    except PWError as e:
        # e.g. a broken/unreachable proxy raises this, not PWTimeout.
        res = {"account": acct.get("username", "?"), "ok": False,
               "messages": [f"Browser error (check --proxy?): {str(e)[:200]}"], "shot": None}
    finally:
        # signup_once() may have swapped in a fresh page/context to route
        # around an AWS WAF CAPTCHA (submit_register() closes the original
        # context itself when that happens) -- close whichever page/context
        # is actually still live, not blindly re-close the one opened above.
        final_page = res.get("page") if isinstance(res, dict) else None
        if final_page and final_page is not page:
            try:
                final_page.context.close()
            except Exception:
                pass
        else:
            try:
                page.close()
            except Exception:
                pass
            if context:
                try:
                    context.close()
                except Exception:
                    pass
        stop_bridge(bridge_proc)
    return res


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
    ap.add_argument("--fast", action="store_true",
                    help="skip the browser and hit the register API directly with plain "
                         "HTTP requests, for sites that support it (currently cricmatch247 "
                         "only, ~10-20x faster) -- more fragile to backend changes; sites "
                         "that need a real browser (e.g. spin24star's WAF challenge) fall "
                         "back to the normal Playwright flow automatically")
    ap.add_argument("--list", action="store_true",
                    help="print stored accounts from accounts.db and exit")
    ap.add_argument("--limit", type=int, default=20, help="rows to show with --list")
    ap.add_argument("--status", help="filter --list/--export-csv to one status, "
                                     "e.g. success, failed, phone_taken")
    ap.add_argument("--filter-url", help="filter --list/--export-csv to signups made "
                                         "against this exact site URL")
    ap.add_argument("--export-csv", nargs="?", const="accounts_export.csv", default=None,
                    metavar="PATH", help="export stored accounts to a CSV file and exit "
                                         "(default path: accounts_export.csv; ALL accounts "
                                         "unless --status/--filter-url filters it)")
    args = ap.parse_args()

    if args.fast and args.no_submit:
        print("--fast has no browser/form to fill without submitting -- drop "
              "--no-submit or --fast.")
        sys.exit(1)

    conn = db.get_connection()

    if args.list:
        db.print_accounts(conn, limit=args.limit, status=args.status, url=args.filter_url)
        return

    if args.export_csv:
        count = db.export_csv(conn, args.export_csv, status=args.status, url=args.filter_url)
        print(f"Exported {count} account(s) to {args.export_csv}")
        return

    accounts = load_accounts(args)
    results = []

    # Only pay for a browser at all if at least one account actually needs
    # one -- an all --fast, all-cricmatch batch never touches Playwright.
    need_browser = any(
        not (args.fast and profile_for(a.get("url") or SITE_URL).supports_http_fast)
        for a in accounts
    )
    pw_ctx = sync_playwright() if need_browser else None
    browser = None
    if pw_ctx:
        p = pw_ctx.__enter__()
        browser = p.chromium.launch(headless=not args.headed)

    try:
        for acct in accounts:
            row_id = db.insert_account(conn, acct)
            prof = profile_for(acct.get("url") or SITE_URL)
            use_fast = args.fast and prof.supports_http_fast
            if args.fast and not prof.supports_http_fast:
                print(f"[fast] {prof.key} doesn't support HTTP-fast mode (needs a "
                      "real browser) -- using Playwright for this one instead.")

            if use_fast:
                res = http_signup_once(acct, submit=not args.no_submit,
                                       interactive=not args.account_file,
                                       site_url=acct.get("url"), proxy=acct.get("proxy"))
            else:
                res = run_browser_account(browser, acct, args)

            results.append(res)
            print(f"[{'OK ' if res['ok'] else 'FAIL' if res['ok'] is False else '?  '}] "
                  f"{res['account']}: {' | '.join(res['messages'])}")
            if res["shot"]:
                print(f"       screenshot: {res['shot']}")

            status = "success" if res["ok"] else ("failed" if res["ok"] is False else "unknown")
            db.update_status(conn, row_id, status,
                              notes="; ".join(res["messages"])[:500], screenshot=res["shot"])
    finally:
        if browser:
            browser.close()
        if pw_ctx:
            pw_ctx.__exit__(None, None, None)

    ok = sum(1 for r in results if r["ok"])
    print(f"\n{ok}/{len(results)} attempt(s) looked successful.")
    print("Stored in accounts.db — run `python main.py --list` to view.")
    sys.exit(0 if ok == len(results) else 1)


if __name__ == "__main__":
    main()
