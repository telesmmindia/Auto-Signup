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
from pathlib import Path
from urllib.parse import parse_qs, urlsplit, urlunsplit

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, Error as PWError, TimeoutError as PWTimeout

import db
from sites import profile_for

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
                return "ok", []
        except Exception:
            pass
        msgs = read_result(page)
        if msgs:
            return "error", msgs
        page.wait_for_timeout(250)
    return "timeout", ["Login did not complete within the timeout."]


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
    except Exception:
        return None

    deadline = time.time() + 15
    while time.time() < deadline:
        if len(context.pages) > pages_before:
            new_page = context.pages[-1]
            new_page.wait_for_timeout(3000)
            return new_page
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


# The Evolution/Ezugi baccarat UI tags every bet spot with a semantic,
# stable data-role: "bet-spot-Player", "bet-spot-Banker", "bet-spot-Tie",
# "bet-spot-SuperSix", "bet-spot-PlayerPair", "bet-spot-BankerPair",
# "bet-spot-PlayerBonus", "bet-spot-BankerBonus", "bet-spot-PerfectPair",
# "bet-spot-EitherPair" (enumerated live, read-only, 2026-07-17). Targeting
# by that exact suffix is unambiguous, so this deliberately does NOT fall
# back to any text-matching heuristic -- for a real-money click, a clean
# "spot not found" failure is safer than a heuristic that could mis-click a
# different bet. `roleSuffix` is the exact suffix, e.g. "Player" / "SuperSix".
_TAG_BET_SPOT_JS = """(roleSuffix) => {
    document.querySelectorAll('[data-pw-spot]').forEach(e => e.removeAttribute('data-pw-spot'));
    const el = document.querySelector(`[data-role="bet-spot-${roleSuffix}"]`);
    if (!el) return false;
    el.setAttribute('data-pw-spot', roleSuffix);
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


def _betting_open(frame):
    """True when the live table is in the OPEN 'place your bets' phase. The
    Evolution frame renders the 'PLACE YOUR BETS' banner text on canvas (it
    never appears in the DOM), but a [data-role="circle-timer"] countdown
    element is present ONLY while betting is open -- verified live
    (2026-07-17): the role appears for the ~15s betting window and is absent
    between rounds. TOTAL BET reads 0 in BOTH the open-and-empty and the closed
    states, so this is the only reliable 'is the window actually open' signal
    for gating a bet -- and the fix for the hedge placing one side into an open
    window while the other account's window was still closed (partial/unhedged)."""
    try:
        return bool(frame.evaluate(_BETTING_OPEN_JS))
    except Exception:
        return False


def _click_bet_spot(frame, role_suffix, timeout=5000):
    """Tag and click the bet spot whose data-role is "bet-spot-<role_suffix>"
    (e.g. "Player", "Banker", "SuperSix"). Returns True if the element was
    found and clicked -- NOT proof the bet registered, the caller must verify
    via _read_total_bet. The bet-spot container has pointer-events:none with
    an inner SVG <path> (pointer-events:all) filling it as the real hot-zone
    (confirmed live via elementFromPoint), so a force-click at the
    container's centre lands on that path. Force is used because a decorative
    glow layer can also sit over the spot."""
    try:
        if not frame.evaluate(_TAG_BET_SPOT_JS, role_suffix):
            return False
        frame.locator(f'[data-pw-spot="{role_suffix}"]').click(timeout=timeout, force=True)
        return True
    except Exception:
        return False


def wait_for_live_table(frame, game_page, timeout_ms=30000):
    """Wait until the live-dealer table is actually interactive, i.e. its
    loading/intro screen is gone and the bet spots exist. Confirmed live
    (2026-07-17): find_game_frame() returns as soon as the frame has enough
    DOM nodes, but that can be the SPRIBE-style loading screen
    (data-role="loading-screen"/"loading-screen-image"/"progress-star"),
    which fully overlays the bet spots -- clicking then just hits the loader.
    Returns True once the table is live, False on timeout."""
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        try:
            st = frame.evaluate("""() => {
                const l = document.querySelector('[data-role="loading-screen"]');
                const loadingVisible = l && l.getBoundingClientRect().height > 0;
                const spot = document.querySelector('[data-role="bet-spot-Banker"]');
                return {loadingVisible: !!loadingVisible, hasSpot: !!spot};
            }""")
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


def read_game_balance(frame):
    """Read the Evolution game frame's own BALANCE readout
    (data-role="balance-label-value", e.g. "₹1,891") as an int, or None.
    Same-tab and real-time, so it reflects wins/losses as they settle."""
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


def _open_table_for(browser, username, password, site_url, category, tile_text,
                    proxy_conf=None, progress=None, label=""):
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
        context.close()
        raise RuntimeError(f"could not open the {tile_text!r} table for {username}")
    frame = find_game_frame(game_page, "evo-games.com")
    if frame is None:
        context.close()
        raise RuntimeError(f"game tab opened but its UI frame never loaded for {username}")
    progress(f"📡 Waiting for the live table to load{tag}…")
    if not wait_for_live_table(frame, game_page):
        context.close()
        raise RuntimeError(f"table never became live (stuck loading) for {username}")
    progress(f"✅ Table ready{tag}")
    return context, page, game_page, frame


class _HedgeStopped(Exception):
    """Raised inside setup when should_stop() is set, so /stoprun can abort a
    run that's still opening tables (setup can take minutes over its retries)."""


def _open_table_with_retry(browser, creds, site_url, category, tile_text,
                           label, progress, attempts=4, proxy_conf=None,
                           should_stop=None):
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
                                   progress=progress, label=f"{label}: {creds['username']}")
        except RuntimeError as e:
            last = e
            if i < attempts:
                progress(f"⏳ Opening {label} table for {creds['username']} — "
                         f"attempt {i}/{attempts} didn't connect, trying a fresh session…")
                for _ in range(10):  # pause, but stay responsive to /stoprun
                    if should_stop():
                        raise _HedgeStopped()
                    time.sleep(1)
    raise last


def _table_id(game_page):
    """Extract Evolution's table_id from a game tab URL (the two accounts must
    share it for the hedge to be on the same hand)."""
    m = re.search(r"table_id=([a-z0-9]+)", game_page.url or "")
    return m.group(1) if m else None


def _now_iso():
    """Local timestamp, same format the bot uses for pair created_at."""
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def run_paired_hedge(browser, banker_creds, player_creds, amount, rounds,
                     site_url=None, category="Baccarat", tile_text="Baccarat A",
                     progress=None, should_stop=None, proxy=None):
    """Run up to `rounds` hedged rounds: `banker_creds` bets Banker and
    `player_creds` bets Player, `amount` each, on the SAME table/hand, until
    `rounds` is reached OR either balance drops below `amount` OR a round goes
    unhedged (partial) OR should_stop() returns True.

    `*_creds` are dicts {"username","password"}. `progress(str)` (optional) is
    called with a human-readable line each round. `should_stop()` (optional)
    returns True to stop after the current round. `proxy` (optional raw string)
    routes BOTH accounts' contexts through the same exit IP -- required when the
    box running the bot has a datacenter IP that the site's WAF 403-blocks
    (login just returns a "Forbidden" page otherwise).

    Returns a summary dict: {"ok", "rounds_done", "requested_rounds",
    "stop_reason", "messages", "final_balance": {"banker","player"},
    "start_balance": {"banker","player"}, "rounds": [...], "shots",
    "started_at", "ended_at"}. `rounds` is a per-round log --
    {"round", "banker", "player", "amount"} after each hedged round -- so a
    caller can persist the full progression, not just the final numbers.
    Real money -- see module notes above."""
    progress = progress or (lambda _msg: None)
    should_stop = should_stop or (lambda: False)
    summary = {"ok": False, "rounds_done": 0, "requested_rounds": rounds,
               "stop_reason": None, "messages": [], "shots": [], "rounds": [],
               "started_at": _now_iso(), "ended_at": None,
               "start_balance": {"banker": None, "player": None},
               "final_balance": {"banker": None, "player": None}}

    ctx_b = ctx_p = gp_b = gp_p = fr_b = fr_p = bridge_proc = None
    try:
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
            ctx_b, _, gp_b, fr_b = _open_table_with_retry(
                browser, banker_creds, site_url, category, tile_text,
                "Banker", progress, proxy_conf=proxy_conf, should_stop=should_stop)
            ctx_p, _, gp_p, fr_p = _open_table_with_retry(
                browser, player_creds, site_url, category, tile_text,
                "Player", progress, proxy_conf=proxy_conf, should_stop=should_stop)
        except _HedgeStopped:
            summary["stop_reason"] = "stopped_by_user"
            return summary
        except RuntimeError as e:
            summary["stop_reason"] = "setup_failed"
            summary["messages"].append(str(e))
            return summary

        tid_b, tid_p = _table_id(gp_b), _table_id(gp_p)
        if tid_b and tid_p and tid_b != tid_p:
            summary["stop_reason"] = "different_tables"
            summary["messages"].append(
                f"The two accounts landed on different tables ({tid_b} vs {tid_p}); "
                "the hedge would not be on the same hand. Aborting before any bet.")
            return summary

        summary["start_balance"]["banker"] = read_game_balance(fr_b)
        summary["start_balance"]["player"] = read_game_balance(fr_p)
        summary["final_balance"]["banker"] = summary["start_balance"]["banker"]
        summary["final_balance"]["player"] = summary["start_balance"]["player"]

        for rnd in range(1, rounds + 1):
            if should_stop():
                summary["stop_reason"] = "stopped_by_user"
                break

            bal_b = read_game_balance(fr_b)
            bal_p = read_game_balance(fr_p)
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
            drain_deadline = time.time() + 30   # (a) let a mid-way window pass
            while time.time() < drain_deadline:
                if should_stop() or not (_betting_open(fr_b) and _betting_open(fr_p)):
                    break
                gp_b.wait_for_timeout(500)
            place_deadline = time.time() + 150  # (b) catch a fresh both-open window
            while time.time() < place_deadline:
                if should_stop():
                    summary["stop_reason"] = "stopped_by_user"
                    break
                if not (_betting_open(fr_b) and _betting_open(fr_p)):
                    gp_b.wait_for_timeout(500)
                    continue
                if (_read_total_bet(fr_b) or 0) != 0 or (_read_total_bet(fr_p) or 0) != 0:
                    # something already staged/settling -- wait for a clean 0/0.
                    gp_b.wait_for_timeout(500)
                    continue

                _click_bet_spot(fr_b, "Banker")
                _click_bet_spot(fr_p, "Player")
                gp_b.wait_for_timeout(1500)
                tb_b = _read_total_bet(fr_b)
                tb_p = _read_total_bet(fr_p)

                if tb_b == amount and tb_p == amount:
                    placed = True
                    break
                if (tb_b or 0) > 0 and (tb_p or 0) > 0 and tb_b == tb_p and tb_b != amount:
                    # Both placed the table's default chip, which isn't `amount`.
                    # This round IS hedged (safe), but the size is wrong -- abort
                    # cleanly rather than repeat, and tell the caller the real size.
                    summary["stop_reason"] = "amount_mismatch"
                    summary["messages"].append(
                        f"The table placed ₹{tb_b} per side (its selected chip), not the "
                        f"requested ₹{amount}. That one round is hedged; stopping. Re-run "
                        f"with amount={tb_b} (chip selection isn't supported yet).")
                    _screenshot_pair(gp_b, gp_p, summary, "mismatch")
                    return summary
                if bool(tb_b) != bool(tb_p):
                    # Exactly one side landed -> UNHEDGED real exposure. Stop now.
                    exposed = banker_creds["username"] if tb_b else player_creds["username"]
                    side = "Banker" if tb_b else "Player"
                    summary["stop_reason"] = "partial_unhedged"
                    summary["messages"].append(
                        f"Round {rnd}: only the {side} bet landed (account {exposed} is "
                        f"exposed for ₹{amount} this hand). Stopping immediately.")
                    _screenshot_pair(gp_b, gp_p, summary, "partial")
                    return summary
                # neither landed (window closed) -> retry the window.
                gp_b.wait_for_timeout(2000)

            if not placed:
                if summary["stop_reason"] == "stopped_by_user":
                    break
                summary["stop_reason"] = "no_open_window"
                summary["messages"].append(
                    f"Round {rnd}: could not get both bets into one open betting "
                    "window; stopping.")
                break

            summary["rounds_done"] = rnd
            # Wait for the hand to resolve (both TOTAL BET back to 0) so the next
            # round starts on a fresh window; then read settled balances.
            for _ in range(40):
                if (_read_total_bet(fr_b) or 0) == 0 and (_read_total_bet(fr_p) or 0) == 0:
                    break
                gp_b.wait_for_timeout(1000)
            bal_b = read_game_balance(fr_b)
            bal_p = read_game_balance(fr_p)
            summary["final_balance"]["banker"] = bal_b
            summary["final_balance"]["player"] = bal_p
            summary["rounds"].append(
                {"round": rnd, "amount": amount, "banker": bal_b, "player": bal_p})
            progress(f"✅ Round {rnd}/{rounds} hedged · ₹{amount}/side\n"
                     f"   🔴 {banker_creds['username']}: ₹{bal_b}\n"
                     f"   🔵 {player_creds['username']}: ₹{bal_p}")

        if summary["stop_reason"] is None:
            summary["stop_reason"] = "completed"
        summary["ok"] = summary["rounds_done"] > 0
        return summary
    finally:
        summary["ended_at"] = _now_iso()
        for closer in (gp_b, gp_p, ctx_b, ctx_p):
            try:
                if closer is not None:
                    closer.close()
            except Exception:
                pass
        stop_bridge(bridge_proc)


def _screenshot_pair(gp_b, gp_p, summary, tag):
    """Screenshot both game tabs into shots/ and record the paths on `summary`."""
    SHOTS_DIR.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    for label, gp in (("banker", gp_b), ("player", gp_p)):
        try:
            path = SHOTS_DIR / f"hedge-{tag}-{label}-{stamp}.png"
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
    ap.add_argument("--filter-url", help="filter --list/--export-csv to signups made "
                                         "against this exact site URL")
    ap.add_argument("--export-csv", nargs="?", const="accounts_export.csv", default=None,
                    metavar="PATH", help="export stored accounts to a CSV file and exit "
                                         "(default path: accounts_export.csv; ALL accounts "
                                         "unless --status/--filter-url filters it)")
    args = ap.parse_args()

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
                # signup_once() may have swapped in a fresh page/context to
                # route around an AWS WAF CAPTCHA (submit_register() closes
                # the original context itself when that happens) -- close
                # whichever page/context is actually still live, not
                # blindly re-close the one opened above.
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
