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
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlsplit, urlunsplit

import requests
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, Error as PWError, TimeoutError as PWTimeout

import db
from chip_plan import group_plan, plan_stake
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
               "kavya", "meera", "shreya", "tanvi", "isha", "aarohi",
               "rudra", "atharv", "vivan", "aadi", "yash", "harsh", "nikhil",
               "siddharth", "varun", "akash", "gaurav", "manish", "suresh",
               "rajesh", "sanjay", "deepak", "ravi", "ankit", "abhishek",
               "tarun", "naveen", "pranav", "kunal", "rohit", "vishal",
               "saurabh", "gautam", "anand", "mohit", "raj",
               "aditi", "kritika", "jiya", "avni", "khushi", "simran", "tanya",
               "nisha", "swati", "ritu", "pallavi", "sonal", "deepika",
               "madhuri", "sunita", "geeta", "anjali", "komal", "preeti",
               "rekha", "sarita", "vandana", "alka", "bharti", "chitra",
               "divya", "ekta", "falguni", "garima", "hina",
               "aarush", "veer", "shaurya", "kian", "laksh", "arhan",
               "agastya", "advait", "ansh", "samar", "viraj", "yuvraj",
               "darsh", "ojas", "parth", "shivansh", "tejas", "om", "ronit",
               "mayank", "nakul", "chirag", "rishabh", "sameer", "pratik",
               "ajay", "sunil", "vinod", "ramesh", "dinesh", "alok", "ashok",
               "prakash", "sandeep", "umesh", "vijay", "rakesh", "jitendra",
               "mahesh",
               "zara", "anaya", "ira", "siya", "navya", "prisha", "amara",
               "disha", "arya", "nandini", "radhika", "shweta", "poonam",
               "seema", "rani", "usha", "mamta", "lata", "kamla", "indira",
               "shanta", "manisha", "jyoti", "archana", "vidya", "asha",
               "shobha", "rajni", "kalpana", "savita"]
LAST_NAMES = ["sharma", "verma", "gupta", "singh", "kumar", "patel", "reddy",
              "nair", "iyer", "rao", "mehta", "joshi", "agarwal", "bhatt",
              "choudhary", "malhotra", "kapoor", "chatterjee", "mukherjee",
              "banerjee", "das", "dutta", "pillai", "menon", "naidu",
              "shetty", "hegde", "bose", "sinha", "tiwari",
              "yadav", "mishra", "pandey", "thakur", "chauhan", "rathore",
              "chowdhury", "saxena", "srivastava", "tripathi", "dubey",
              "pathak", "trivedi", "bhat", "kaur", "gill", "sandhu", "arora",
              "khanna", "chopra", "bajaj", "jain", "shah", "desai", "goel",
              "garg", "mittal", "aggarwal", "bansal", "kulkarni", "deshmukh",
              "pawar", "gaikwad", "jadhav",
              "bhatia", "ahluwalia", "sethi", "kohli", "khurana", "walia",
              "anand", "chandra", "bhardwaj", "tandon", "mahajan", "bakshi",
              "grover", "batra", "ghosh", "sen", "roy", "dey", "saha",
              "ganguly", "chakraborty", "bhattacharya", "iyengar",
              "subramaniam", "krishnan", "venkatesh", "raman", "natarajan",
              "gowda", "shenoy", "kamath", "prabhu", "bhandari", "rawat",
              "bisht", "negi", "panchal", "rana", "solanki", "chavan",
              "more", "kale", "salunkhe", "nikam"]
EMAIL_DOMAIN = "gmail.com"

# Per-site selectors + behavior now live in one profile file per site under
# sites/ (sites/cricmatch.py, sites/spin24star.py), selected by URL via
# profile_for(). Engine helpers read `profile_for(page.url).sel[...]` instead of
# a shared SEL dict -- so adding a site is a one-file change, no engine edits.
# See the "Multi-site support" section of CLAUDE.md.

SHOTS_DIR = Path("shots")


def save_screenshot(target, path):
    """Screenshot-saving is disabled globally (2026-08-01, by request) -- a
    no-op that returns None without touching disk. Every screenshot call
    site in both main.py and telegram_bot.py goes through this one function,
    so it can be re-enabled from a single place instead of restoring ~20
    individual page.screenshot() calls."""
    return None


def gen_password(prof=None):
    """Build a password that satisfies the form policy:
    5-60 chars, >=1 digit, >=1 special, upper and lower case.

    `prof` (a SiteProfile) narrows that to the site's own rule where it is
    stricter. Called with no profile -- which is every pre-winclash call site
    -- the result is byte-for-byte the same shape as before: 10 characters,
    one of each class including a special. winclash caps the field at 12
    characters and has no special-character rule (the check is commented out
    in its own JS), so passing its profile drops the special and keeps the
    length inside its window. Generating to the site's policy matters because
    these forms validate in the browser: a password the JS rejects never
    reaches the server at all, and surfaces as a mystery toast."""
    needs_special = True if prof is None else prof.password_needs_special
    lo = 5 if prof is None else prof.password_min_len
    hi = 60 if prof is None else prof.password_max_len

    pools = [random.choice(string.ascii_uppercase),
             random.choice(string.ascii_lowercase),
             random.choice(string.digits)]
    if needs_special:
        pools.append(random.choice("!@#$%^&*"))
    target = max(lo, min(10, hi))
    pools += random.choices(string.ascii_letters + string.digits,
                            k=max(0, target - len(pools)))
    random.shuffle(pools)
    return "".join(pools)


def gen_account(prof=None):
    """Generate a random test identity. Phone is filled in separately.

    `prof` (a SiteProfile) applies that site's field limits. Only the
    username needs trimming: first+last+tag runs 13-15 characters, which is
    fine everywhere except winclash, whose #userName carries
    pattern="...{5,12}$" and would reject it in the browser. The random tag
    is kept whole and the name part is what gets shortened, so usernames stay
    as distinct as they were. The email is left at full length (its field
    allows 60) so it remains unique even when two trimmed usernames collide."""
    first = random.choice(FIRST_NAMES)
    last = random.choice(LAST_NAMES)
    tag = random.randint(100, 9999)
    username = f"{first}{last}{tag}"
    max_len = 0 if prof is None else prof.username_max_len
    if max_len and len(username) > max_len:
        username = f"{first}{last}"[:max_len - len(str(tag))] + str(tag)
    email = f"{first}.{last}{tag}@{EMAIL_DOMAIN}"
    return {"username": username, "email": email, "password": gen_password(prof)}


def gen_free_phone():
    """A throwaway Indian-format mobile number (10 digits, starts 6-9) to swap
    an account onto post-signup so its real signup phone can be reused -- see
    free_phone_number()/http_free_phone_number()."""
    return random.choice("6789") + "".join(random.choices(string.digits, k=9))


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


def _context_proxy_conf(proxy, proxy_conf=None):
    """The proxy dict a REPLACEMENT browser context should be given.

    Prefer `proxy_conf`, the config the caller is already running on. It
    matters that this is not re-derived from the raw proxy string: for a
    SOCKS5 proxy with credentials the caller's conf points at a local pproxy
    bridge (maybe_bridge_proxy), because **Chromium cannot authenticate to
    SOCKS5 at all**. Re-parsing the raw string would hand the new context the
    upstream SOCKS5 URL that Chromium can't use, so a WAF retry would break
    on exactly the proxies the bridge exists for -- while looking fine on the
    http:// proxy in use today.

    Falls back to parsing the raw string when no conf was passed, which is
    what every caller did before, so nothing regresses."""
    if proxy_conf:
        return proxy_conf
    try:
        return parse_proxy(proxy) if proxy else None
    except ValueError:
        return None


def new_site_context(browser, site_url=None, proxy_conf=None):
    """Open a BrowserContext carrying whatever the target site needs, rather
    than a bare browser.new_context().

    Today that is only the user agent, and only for sites whose profile sets
    one (winclash: headless Chromium's default "HeadlessChrome" UA is
    flat-403'd by its WAF before any page is served -- see
    SiteProfile.user_agent). Sites that set nothing get exactly the context
    they got before, so this is a no-op for cricmatch/khelofun/spin24star/
    starexch. Kept as one function so a new site's context requirements are
    added in one place instead of at each new_context() call site."""
    kwargs = {}
    if proxy_conf:
        kwargs["proxy"] = proxy_conf
    ua = profile_for(site_url or SITE_URL).user_agent
    if ua:
        kwargs["user_agent"] = ua
    return browser.new_context(**kwargs)


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
    # A page_url site's form is on ANOTHER page, so anything overlaying this
    # one is about to be navigated away from -- closing it first is pure cost
    # (dismiss_popups sleeps 800ms and each candidate click waits up to 1.5s).
    if prof.register_trigger != "page_url":
        dismiss_popups(page)

    # Already showing? (Khelo sites open the form directly at /?reg=1.)
    try:
        if page.locator(prof.sel["username"]).first.is_visible():
            return True
    except Exception:
        pass

    # winclash: the signup form is its OWN PAGE (/join-now), so there is no
    # JOIN button to hunt for -- navigate to it. This deliberately runs AFTER
    # the caller has already loaded the homepage: the whole site sits behind
    # an AWS WAF challenge, and it is that homepage load which runs
    # challenge.js and mints the `aws-waf-token` cookie. Confirmed live --
    # going straight to /join-now in a fresh context comes back a bare 403
    # ("403 Forbidden" as the page title), while the same navigation after a
    # homepage load renders the real form. The retry covers the token not
    # being minted yet by the time we navigate.
    if prof.register_trigger == "page_url":
        target = urljoin(page.url or "", prof.register_path)
        for attempt in range(2):
            try:
                page.goto(target, wait_until="domcontentloaded", timeout=60000)
                # An AWS WAF wall can land on THIS navigation even when the
                # homepage came through clean -- confirmed live: winclash
                # answered / with 202 and /join-now with 405
                # `x-amzn-waf-action: captcha` in the same session. The soft
                # "challenge" action clears itself once the page's own
                # challenge.js has run, so wait it out before concluding the
                # form is missing. The hard "captcha" action never clears on
                # its own; that one is left to the caller
                # (open_signup_form() -> ensure_waf_cleared()), which is why
                # this returns False rather than raising.
                # A wall still up after waiting it out is the hard
                # "captcha" action, which never clears on its own. Hand back
                # at once so open_signup_form() can solve (or reuse a token)
                # instead of burning the selector wait and a second attempt
                # against a page that cannot become the form -- measured at
                # ~45s of pure waiting per signup.
                if not wait_out_waf_wall(page):
                    return False
                page.wait_for_selector(prof.sel["username"], state="visible",
                                       timeout=15000)
                return True
            except Exception:
                if attempt == 0:
                    page.wait_for_timeout(5000)
        return False

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


# --- reusing a solved AWS WAF token --------------------------------------
# A CapSolver solve is the single most expensive step in a winclash signup:
# measured on the production server, 33 of the 50 seconds it takes to reach a
# filled form, and it is billed every time. AWS WAF tokens are valid for
# MINUTES, not for one request, so a token solved for one signup is very
# likely still good for the next few. Caching one turns that 33s (and its
# cost) into a page load for every signup that follows, and a stale token is
# self-healing: the wall simply shows again and a real solve happens.
#
# Keyed by host AND the egress it was minted from, because AWS WAF tokens can
# be IP-bound -- the same reason _capsolver_proxy() exists. A proxy change
# therefore misses the cache rather than reusing a token from another IP.
# How long a solved AWS WAF token is worth re-presenting. Deliberately
# optimistic rather than conservative: a token that has expired costs exactly
# one page load to find out (the wall shows, ensure_waf_cleared() drops it and
# solves for real), while a token retired too early costs a full CapSolver
# solve -- measured at ~21s of a 27.7s cold signup against winclash, versus
# 6.1-6.6s for a signup that reused one. Env-overridable either way.
_WAF_TOKEN_TTL_SECS = int(os.environ.get("WAF_TOKEN_TTL_SECS", "600"))
_waf_tokens = {}
_waf_tokens_lock = threading.Lock()


def _waf_token_key(site_url, proxy):
    return (urlsplit(site_url or SITE_URL).hostname or "", proxy or "direct")


def cache_waf_token(site_url, token, proxy=None):
    """Remember a freshly solved token so the next signup can skip the solve."""
    if not token:
        return
    with _waf_tokens_lock:
        _waf_tokens[_waf_token_key(site_url, proxy)] = (token, time.time())


def cached_waf_token(site_url, proxy=None):
    """A previously solved token for this site+egress, if it is still young
    enough to be worth trying. None otherwise."""
    key = _waf_token_key(site_url, proxy)
    with _waf_tokens_lock:
        entry = _waf_tokens.get(key)
        if not entry:
            return None
        token, at = entry
        if time.time() - at > _WAF_TOKEN_TTL_SECS:
            _waf_tokens.pop(key, None)
            return None
        return token


def forget_waf_token(site_url, proxy=None):
    """Drop a token that turned out not to work, so nothing retries it."""
    with _waf_tokens_lock:
        _waf_tokens.pop(_waf_token_key(site_url, proxy), None)


def seed_waf_token(context, site_url=None, proxy=None):
    """Put an already-solved token into a BRAND-NEW context before its first
    navigation, so a site whose every page load is walled (winclash) is served
    the real site on the very first GET. Returns True if one was injected.

    This is the cheap half of the token cache. Without it a warm signup still
    pays: load the homepage -> hit the wall -> throw that context away ->
    build a fresh one with the token -> load the homepage AGAIN. That second
    load is pure waste, because the fresh context this token needs is the one
    we are about to create anyway. A stale token costs nothing extra: the wall
    simply shows and ensure_waf_cleared() runs exactly as before."""
    token = cached_waf_token(site_url, proxy)
    if not token:
        return False
    try:
        apply_waf_token(context, site_url or SITE_URL, token)
        return True
    except Exception:
        return False


def current_waf_token(page):
    """The aws-waf-token this context is carrying, if any -- either seeded by
    seed_waf_token() or minted by the site's own challenge.js."""
    try:
        for c in page.context.cookies():
            if c.get("name") == "aws-waf-token" and c.get("value"):
                return c["value"]
    except Exception:
        pass
    return None


def has_waf_token_cookie(page):
    """True once this context holds an aws-waf-token cookie -- i.e. the site's
    own challenge.js has run and minted one."""
    return bool(current_waf_token(page))


def post_load_settle(page, site_url=None, ms=4000, poll_ms=250):
    """The pause after the first page load, before the signup form is looked
    for. Sites whose form is a MODAL keep the flat sleep byte-for-byte -- it
    is load-bearing there (a promo overlay covers the JOIN button while
    Playwright already reports it visible, and replacing this with a
    visibility wait reproduced a real failure).

    Sites whose form is its own PAGE (winclash) are about to navigate away, so
    the only thing this wait can buy them is letting challenge.js mint the
    aws-waf-token cookie the next navigation needs. That is a condition we can
    actually test for, so poll for it and leave the moment it is true --
    typically well under the flat 4s, and instantly when a cached token was
    seeded into the context."""
    prof = profile_for(site_url or page.url or SITE_URL)
    if prof.register_trigger != "page_url":
        page.wait_for_timeout(ms)
        return
    if not prof.waf_on_navigation:
        return
    deadline = time.time() + ms / 1000.0
    while time.time() < deadline:
        if not waf_wall_showing(page) and has_waf_token_cookie(page):
            return
        page.wait_for_timeout(poll_ms)


def signup_entry_url(site_url=None, token_seeded=False):
    """Where a signup's FIRST navigation should go.

    Normally the site root: a page_url site (winclash) needs the homepage load
    to run challenge.js and mint the aws-waf-token cookie, and fetching
    /join-now cold in a fresh context comes back a bare 403 -- so the homepage
    hop is what makes the register page reachable at all.

    When a solved token has already been seeded into this context
    (seed_waf_token), that hop has nothing left to do and the register page
    can be loaded directly, saving a whole page load per signup. If the token
    turns out to be stale the wall shows on the register page instead of the
    homepage, and open_signup_form() -> ensure_waf_cleared() recovers exactly
    as it always did -- so the worst case is the old behaviour, one navigation
    later."""
    site = site_url or SITE_URL
    prof = profile_for(site)
    if token_seeded and prof.register_trigger == "page_url" and prof.register_path:
        return urljoin(site, prof.register_path)
    return site


def restart_with_waf_token(page, site_url, token, proxy=None, proxy_conf=None,
                           settle_ms=4000):
    """Open a FRESH context carrying `token`, close the old one, and load the
    site there. Returns (page, cleared).

    The fresh context is not an optimisation, it is the whole trick: a token
    injected into the very context that was challenged is still refused --
    AWS WAF tracks session state beyond the cookie -- while the identical
    token in a clean context works immediately. Established live for the
    register-POST wall and reused for every wall since."""
    old_context = page.context
    browser = old_context.browser
    conf = _context_proxy_conf(proxy, proxy_conf)
    new_context = new_site_context(browser, site_url, conf)
    apply_waf_token(new_context, site_url or SITE_URL, token)
    new_page = new_context.new_page()
    try:
        old_context.close()
    except Exception:
        pass
    try:
        new_page.goto(site_url or SITE_URL, wait_until="domcontentloaded",
                      timeout=60000)
    except PWError:
        return new_page, False
    # Poll rather than sleep the whole budget: the point of this wait is to
    # find out whether the token worked, and on a good token the wall is gone
    # as soon as the document parses.
    deadline = time.time() + settle_ms / 1000.0
    while time.time() < deadline:
        if not waf_wall_showing(new_page):
            return new_page, True
        new_page.wait_for_timeout(250)
    return new_page, not waf_wall_showing(new_page)


def apply_waf_token(context, website_url, token):
    """Inject a solved aws-waf-token into a BrowserContext so the next request
    to the site carries it."""
    host = urlsplit(website_url).hostname
    context.add_cookies([{"name": "aws-waf-token", "value": token,
                          "domain": host, "path": "/"}])


def waf_wall_showing(page):
    """True when the page currently displaying is AWS WAF's "Human
    Verification" interstitial rather than the site.

    Two signals, and BOTH are needed:

    * window.gokuProps -- set by an inline <script> in the interstitial, so
      it is readable the instant the document parses. Verified against saved
      copies of the genuine winclash homepage and /join-now: the string
      appears in neither, so it is specific to the wall despite the site
      loading AWS WAF's challenge.js on every page.
    * #captcha-container / #amzn-captcha-verify-button -- the interstitial's
      own roots, injected later by its script.

    Checking only the ids left a real false-negative window: right after
    navigation the interstitial's HTML has arrived but its script has not run
    yet, so the ids are absent and the wall reads as "clear" -- which is how
    a walled signup got reported as a missing JOIN button rather than as a
    block."""
    try:
        return bool(page.evaluate(
            "() => !!(window.gokuProps"
            " || document.getElementById('captcha-container')"
            " || document.getElementById('amzn-captcha-verify-button'))"))
    except Exception:
        return False


def ensure_waf_cleared(page, site_url=None, proxy=None, settle_secs=12,
                       proxy_conf=None, force=False):
    """Get past an AWS WAF interstitial served on page LOAD, and return the
    page to carry on with. Returns (page, ok, message).

    This is a DIFFERENT wall from the one submit_register() handles. That one
    arrives as the response to the register POST; this one is in front of
    ordinary navigation -- winclash answers a plain GET of its homepage with
    HTTP 202, `x-amzn-waf-action: challenge`, and a page whose only content
    is "Let's confirm you are human". Nothing on the site is reachable until
    it is cleared, so this runs before the signup form is ever looked for.

    Usually nothing needs doing and nothing is spent: that action is designed
    to be cleared silently by the site's own challenge.js, which a real
    browser runs in a few seconds. So this WAITS and re-checks first, and
    only reaches for a paid CapSolver solve if the wall is still up after
    `settle_secs`.

    `page` may come back as a NEW page in a NEW context. A solved token
    injected into the very context that was challenged is still refused --
    established live for the register-POST wall (see submit_register()'s
    docstring); AWS WAF tracks session state beyond the cookie -- so the
    retry starts from a clean context. Callers MUST switch to the returned
    page: when it is a new one, the original context has been closed here."""
    site = site_url or SITE_URL

    if force:
        # The caller was blocked on an XHR (405 + x-amzn-waf-action: captcha),
        # which leaves the PAGE untouched -- so there is no interstitial in
        # front of us to read challenge parameters from, and the check below
        # would answer "all clear" while every API call stays blocked. A
        # fresh navigation is what surfaces the wall (and, if the soft
        # challenge answers instead, mints the token the XHR was missing).
        try:
            page.goto(site, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(3000)
        except PWError:
            pass

    deadline = time.time() + settle_secs
    while time.time() < deadline:
        if not waf_wall_showing(page):
            return page, True, ""
        # A wall is up and a usable token is already in hand -- there is
        # nothing to gain by waiting to see whether this one clears itself.
        if cached_waf_token(site, proxy):
            break
        page.wait_for_timeout(1000)
    if not waf_wall_showing(page):
        return page, True, "AWS WAF challenge cleared itself"

    # A token solved for an earlier signup is very likely still valid, and
    # trying it costs a page load instead of a paid solve. If it has expired
    # the wall just shows again and the real solve below runs, so the worst
    # case is one extra navigation.
    reused = cached_waf_token(site, proxy)
    # If this very token is already in the context and the wall is STILL up,
    # it has just been disproved -- rebuilding a context to present it again
    # would only spend another page load to reach the same answer. Drop it and
    # go straight to a real solve. (This is what keeps a generous
    # WAF_TOKEN_TTL_SECS cheap: an over-optimistic TTL costs nothing beyond
    # the load that discovered it, instead of a second one.)
    if reused and reused == current_waf_token(page):
        forget_waf_token(site, proxy)
        reused = None
    if reused:
        page, cleared = restart_with_waf_token(page, site, reused, proxy, proxy_conf)
        if cleared:
            return page, True, "AWS WAF cleared with a reused token"
        forget_waf_token(site, proxy)

    if not capsolver_key():
        return page, False, ("AWS WAF is showing a Human Verification page and "
                             "CAPSOLVER_API_KEY is not set, so it can't be solved")
    try:
        challenge = parse_aws_waf_challenge(page.content())
        if not challenge:
            return page, False, ("AWS WAF Human Verification page is up but its "
                                 "challenge parameters could not be read")
        token = solve_aws_waf_token(site, challenge, proxy=proxy)
    except (RuntimeError, urllib.error.URLError, PWError) as e:
        return page, False, f"AWS WAF solve failed: {str(e)[:200]}"

    new_page, cleared = restart_with_waf_token(page, site, token, proxy, proxy_conf)
    if not cleared:
        return new_page, False, ("AWS WAF still challenging after injecting a "
                                 "solved token")
    cache_waf_token(site, token, proxy)
    return new_page, True, "AWS WAF cleared with a CapSolver token"


def wait_out_waf_wall(page, secs=15):
    """Give AWS WAF's soft "challenge" action time to clear itself, which is
    what happens once the page's own challenge.js finishes. Returns True if
    no wall is showing by the end. Costs nothing on a site that never serves
    one -- the first check already answers False."""
    deadline = time.time() + secs
    while waf_wall_showing(page):
        if time.time() >= deadline:
            return False
        page.wait_for_timeout(1000)
    return True


def open_signup_form(page, site_url=None, proxy=None, proxy_conf=None):
    """Get the signup form on screen, clearing an AWS WAF wall on the way if
    one turns up. Returns (ok, page, message).

    Shared by the CLI (signup_once) and the Telegram bot
    (_blocking_fill_and_register) so the two can't drift, same reasoning as
    fill_register_form. `page` may be a NEW page in a NEW context -- see
    ensure_waf_cleared() -- so callers MUST rebind to whatever comes back.

    The order matters: try normally first, and only look at the WAF if that
    failed. A site whose form simply isn't where the profile says still
    reports the plain "couldn't open the form" message rather than a
    misleading WAF one, and no CapSolver credit is ever spent on a
    selector bug."""
    if open_signup_modal(page):
        return True, page, ""
    if not waf_wall_showing(page):
        return False, page, "Could not open the signup modal (JOIN button)."

    # settle_secs=0: open_signup_modal already waited this wall out, so it is
    # the hard "captcha" action, which never clears on its own.
    page, waf_ok, waf_msg = ensure_waf_cleared(page, site_url, proxy=proxy,
                                               settle_secs=0, proxy_conf=proxy_conf)
    if not waf_ok:
        return False, page, waf_msg
    if open_signup_modal(page):
        return True, page, waf_msg
    return False, page, ("Could not open the signup form even after clearing "
                         "the AWS WAF Human Verification page.")


def is_waf_captcha(captured):
    """True if the captured register response was an AWS WAF CAPTCHA block."""
    action = (captured.get("action") or "").lower()
    if action in ("captcha", "challenge"):
        return True
    return "gokuProps" in (captured.get("body") or "")


def normalize_phone(phone, site_url=None):
    """Reduce a typed phone number to the digits the site's mobile field can
    actually hold. Returns the cleaned number (or the original text if there
    is nothing sensible to do with it).

    This exists because the failure it prevents is INVISIBLE. The register
    form's mobile <input> carries maxlength, so a browser silently drops
    anything past it: 919304199756 typed into winclash's 10-character field
    becomes 9193041997. Confirmed live 2026-08-29. The consequences all look
    like something else --
      * the site never reports "this mobile number is already in use",
        because the truncated number genuinely isn't registered;
      * the OTP is sent to a number nobody is holding, so the signup dies at
        the OTP step as "wrong/expired code";
      * a phone POOL stops rotating usefully, since every entry maps to a
        different wrong number;
      * and the database records what was ASKED for, not what landed, so
        none of it shows up in the logs.
    Strips separators, a leading +country code, and a trunk 0."""
    prof = profile_for(site_url or SITE_URL)
    digits = re.sub(r"\D", "", str(phone or ""))
    want = prof.phone_digits or 0
    cc = prof.phone_country_code or ""
    if want and len(digits) > want:
        if cc and digits.startswith(cc) and len(digits) - len(cc) == want:
            digits = digits[len(cc):]
        elif digits.startswith("0") and len(digits) - 1 == want:
            digits = digits[1:]
    return digits or str(phone or "")


def fill_register_form(page, acct):
    """Fill the 4 register fields and ensure the T&C box is checked. Assumes
    the register form/modal is already open. Shared by the initial fill and the
    post-WAF-solve refill so they can't drift."""
    prof = profile_for(page.url)
    phone = normalize_phone(acct["phone"], page.url)
    for sel, value in [(prof.sel["username"], acct["username"]),
                       (prof.sel["email"], acct["email"]),
                       (prof.sel["password"], acct["password"]),
                       (prof.sel["phone"], str(phone))]:
        field = page.locator(sel)
        field.click()
        field.press_sequentially(value, delay=30)
        field.blur()
    # Read the phone back. A maxlength on the field silently truncates a
    # too-long number, and every downstream symptom of that points somewhere
    # else (see normalize_phone). Refusing here costs one attempt; not
    # refusing sends an OTP to a stranger's phone and looks like a bad code.
    try:
        landed = page.input_value(prof.sel["phone"])
    except Exception:
        landed = str(phone)
    if landed != str(phone):
        raise ValueError(
            f"the signup form would not accept the phone number: asked for "
            f"{phone}, the field kept {landed!r} (it holds "
            f"{prof.phone_digits} digits). Enter the number without a country "
            f"code.")
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


def submit_register(page, acct, site_url, proxy=None, proxy_conf=None):
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
        # Share it with every other WAF path, so the next signup's navigation
        # wall costs a page load rather than another paid solve.
        cache_waf_token(site_url or SITE_URL, token, proxy)

        old_context = page.context
        browser = old_context.browser
        proxy_conf = _context_proxy_conf(proxy, proxy_conf)
        new_context = new_site_context(browser, site_url, proxy_conf)
        apply_waf_token(new_context, site_url or SITE_URL, token)
        new_page = new_context.new_page()
        try:
            old_context.close()
        except Exception:
            pass

        # The token is already in this context, so a page_url site can go
        # straight back to its register page rather than by way of the
        # homepage -- same reasoning as signup_entry_url()'s.
        new_page.goto(signup_entry_url(site_url, True),
                      wait_until="domcontentloaded", timeout=60000)
        post_load_settle(new_page, site_url)
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


def _otp_screen_showing(page):
    """True when the signup OTP entry field is on screen."""
    try:
        return page.locator(profile_for(page.url).sel["otp_digits"]).first.is_visible()
    except Exception:
        return False


def _looks_like_phone_taken(page, msgs):
    """True when a toast-only site is really saying 'that mobile number is
    already registered'. cricmatch has a dedicated element for this
    (check_phone_taken); sites without one list the wording they use in
    `phone_taken_texts`, and sites that list nothing -- every site that
    predates this -- never match, so their behaviour is unchanged."""
    texts = profile_for(page.url).phone_taken_texts
    if not texts:
        return False
    joined = " ".join(msgs).lower()
    return any(t.lower() in joined for t in texts)


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
        if _otp_screen_showing(page):
            return "otp", []
        msgs = read_result(page)
        if msgs and not _looks_like_otp_sent(msgs):
            # One last look before calling it a rejection. On a site whose
            # "we sent you a code" toast is worded so that
            # _looks_like_otp_sent() misses it, the OTP screen itself is the
            # authoritative signal -- and it renders in the same JS callback
            # as the toast, so it is already there. This can only ever turn a
            # would-be error into a real, visible OTP screen, never the
            # reverse, so no existing site's behaviour changes.
            if _otp_screen_showing(page):
                return "otp", []
            if _looks_like_phone_taken(page, msgs):
                return "phone_taken", msgs
            return "error", msgs
        page.wait_for_timeout(poll_ms)
    return "timeout", []


def wait_for_otp_outcome(page, timeout_ms=None, poll_ms=250):
    """After clicking the OTP Verify button, poll for either the inline OTP
    error appearing or the OTP screen closing (success), instead of a flat
    sleep.

    `timeout_ms=None` takes the site's own budget (otp_outcome_timeout_ms,
    10s by default, unchanged for every site that verifies in one call).
    winclash needs far longer because one Verify click is two round trips
    plus a navigation -- see its profile."""
    if timeout_ms is None:
        timeout_ms = profile_for(page.url).otp_outcome_timeout_ms
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        prof = profile_for(page.url)
        try:
            err_sel = prof.sel.get("otp_error")
            if err_sel:
                e = page.locator(err_sel).first
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


def otp_digit_count(page):
    """How many digits the signup OTP has, or 0 if the OTP screen isn't up.

    Two shapes, hence the profile flag: cricmatch/spin24star render one
    single-character box PER DIGIT, so counting the elements IS the answer
    and stays right even if a site changes code length. winclash renders ONE
    <input maxlength=6>, where the element count is always 1 no matter how
    long the code is -- counting there would ask the operator for a 1-digit
    OTP. Shared by the CLI and the Telegram bot so the two can't drift."""
    prof = profile_for(page.url)
    try:
        n = page.locator(prof.sel["otp_digits"]).count()
    except Exception:
        return 0
    if not n:
        return 0
    return prof.otp_length if prof.otp_mode == "single" else n


def fill_otp(page, otp):
    """Type `otp` into whichever OTP widget this site uses. Returns True if
    it went in. Typed rather than filled so the widget's own input handlers
    fire (winclash's oninput="validateOTP(this)", cricmatch's auto-advance)."""
    prof = profile_for(page.url)
    boxes = page.locator(prof.sel["otp_digits"])
    try:
        if not boxes.count():
            return False
        if prof.otp_mode == "single":
            box = boxes.first
            box.click()
            box.press_sequentially(str(otp), delay=40)
        else:
            for i, ch in enumerate(str(otp)):
                box = boxes.nth(i)
                box.click()
                box.press_sequentially(ch, delay=40)
    except Exception:
        return False
    page.wait_for_timeout(500)
    return True


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

    n = otp_digit_count(page)
    if n == 0:
        result["messages"].append("OTP screen detected but no digit inputs found.")
        return result

    print(f"\nOTP screen is up — an SMS code was sent to {acct.get('phone', 'your phone')}.")
    otp = prompt_otp(n)

    if not fill_otp(page, otp):
        result["messages"].append("Could not type the OTP into the form.")
        return result

    stamp = time.strftime("%Y%m%d-%H%M%S")
    otp_filled = SHOTS_DIR / f"{acct['username']}-{stamp}-otp-filled.png"
    save_screenshot(page, otp_filled)

    if not click_first_visible(page, prof.sel["otp_verify"], timeout=6000):
        result["messages"].append("Could not find a visible Verify button.")
        result["shot"] = save_screenshot(page, otp_filled)
        return result
    outcome = wait_for_otp_outcome(page)

    otp_result = SHOTS_DIR / f"{acct['username']}-{stamp}-otp-result.png"
    result["shot"] = save_screenshot(page, otp_result)

    # Did the site reject the OTP?
    err = ""
    if outcome == "error":
        try:
            err_sel = prof.sel.get("otp_error")
            if err_sel:
                e = page.locator(err_sel).first
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


# Retry budget for freeing the signup phone number. Started at 4x10s (~40s)
# but that wasn't enough for a real failure mode: a bare 403 Forbidden (no
# JSON body -- unlike the app-level 500 covered above, this looks like an
# edge/WAF-level block, e.g. from calling this endpoint too rapidly), which
# needs meaningfully longer to clear than a transient app error does. Bumped
# to 10x20s (~200s / ~3.3min worst case), then bumped again to 15x45s
# (~630s / ~10.5min worst case) after a real /freenum call still hit the same
# bare 403 and exhausted the whole 10x20s budget without it clearing -- so a
# signup/freenum only gives up on this step after a real chance to ride out a
# longer rate-limit window -- the whole point of this feature is guaranteeing
# the number is free before the NEXT signup in a continuous run tries to use
# it again, so a retry budget that gives up early defeats that purpose.
FREE_NUMBER_MAX_ATTEMPTS = 15
FREE_NUMBER_RETRY_COOLDOWN_SECS = 45

# How many chip clicks run_paired_hedge may spend building ONE side's stake
# when the requested amount isn't a single chip value (see chip_plan.plan_stake
# and _place_stake). This is a hard trade against the betting window: Stock
# Market Live's "PLACE YOUR BETS" banner is open only ~10s, both sides click
# concurrently but each denomination switch costs a verified select_chip_fast
# round trip, and a window that closes mid-stake leaves the two sides staked
# UNEQUALLY -- which is a real, unhedged difference, not just a wrong size.
# 6 is roughly 2 chip switches + 6 spot clicks per side. tournament.py runs 8
# inside baccarat's wider ~15s window; this is the tighter game, so it gets a
# smaller budget. Raise it only after a live run shows real timing headroom.
# The cost of a small budget is only which amounts are reachable (₹100,000 on
# a rail topping out at ₹2,500 needs 40 clicks and is refused up front, before
# any money moves), never a half-placed bet.
HEDGE_MAX_BET_CLICKS = int(os.getenv("HEDGE_MAX_BET_CLICKS", "6"))

_FREE_NUMBER_FETCH_JS = """async (args) => {
    const [path, phone] = args;
    const csrf = document.querySelector('meta[name=csrf-token]').content;
    const resp = await fetch(path, {
        method: "POST",
        credentials: "same-origin",
        headers: {
            "Accept": "*/*",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest",
            "DNT": "1",
        },
        body: "_token=" + encodeURIComponent(csrf) + "&phone=" + encodeURIComponent(phone),
    });
    const text = await resp.text();
    return {status: resp.status, text: text};
}"""


def free_phone_number(page, site_url=None):
    """Browser-path counterpart to http_free_phone_number(): call right after
    a signup's OTP verify succeeds, while `page` is still on the
    now-logged-in account. Fires the request as a real in-page `fetch()` via
    page.evaluate() (NOT page.context.request -- see below) to a fresh random
    phone number, which -- confirmed live 2026-07-22 against a real account,
    by comparing the Profile Data page's Mobile Number before and after --
    silently overwrites the account's registered mobile with no further OTP
    step required. Returns (ok, new_phone, message). `new_phone` is returned
    even on failure (the number that was attempted) so the caller can log it
    either way.

    Two things were required to get a real 200 here, both confirmed live:
    1. It MUST be `page.evaluate(fetch(...))`, not `page.context.request.post()`
       -- the latter kept 500ing even with an identical URL/body/headers.
       page.context.request is a separate, out-of-band HTTP client; it shares
       the context's cookie jar but not whatever else a real in-page fetch
       carries (e.g. Chromium's own sec-ch-ua/* client hints), and this
       endpoint's handler apparently depends on something only the real
       in-page request path provides.
    2. The call must happen on an already-"settled" session -- calling it
       immediately after login 500s (generic Laravel "Server Error"); waiting
       ~6s first (letting the page's own background bootstrap calls finish,
       including one that sets a "domain_switch" cookie) made it return a
       clean 200. In the signup flow this is naturally satisfied (the account
       just went through register+OTP-verify, which already takes a while),
       but a short explicit wait is added anyway as a safety margin.

    The real endpoint (found live via manual Tamper Dev request interception,
    after an earlier guess -- "/send_otp", misread off a small phone
    screenshot -- was confirmed wrong, a hard 405) is a fixed path from
    `sites/cricmatch.py`'s `free_number_path`, "/send_otp_touser". Its JSON
    response follows the same {"message","message_class",...} shape as the
    register endpoint (see http_is_error()), so success is judged the same
    way, not just by HTTP status.

    Retries up to FREE_NUMBER_MAX_ATTEMPTS times, waiting
    FREE_NUMBER_RETRY_COOLDOWN_SECS between each, for ANY failure -- not just
    the exec-context-destroyed race above, but also a plain rejection (e.g. a
    transient 500, or a bare 403 -- edge/WAF-level rate-limiting from calling
    this endpoint too rapidly, seen live and needing longer to clear than an
    app-level 500 does). Confirmed live: a real run hit `phone_taken` on its
    NEXT signup because one round's free-number call failed and wasn't
    retried enough, leaving the real number still attached to that account.
    A generous multi-attempt retry budget (see the module-level constants'
    comment for the current worst-case time) gives the site real odds of
    clearing whatever rate limit it's applying, rather than giving up early
    and letting the next signup in a continuous run collide with a number
    that's still taken."""
    prof = profile_for(page.url)
    if not prof.supports_free_number:
        return False, None, f"{prof.key} does not support freeing the signup phone number."

    new_phone = gen_free_phone()
    # Let the session "settle" -- see docstring. Widened from 4s to 8s after a
    # live /newacc run (2026-07-25) still occasionally hit "number already in
    # use" on the round right after a reported free-number success -- the
    # call itself gets a 200, but the site's own backend appears to need more
    # than 4s to make the swap visible to the register endpoint again.
    page.wait_for_timeout(8000)
    parts = urlsplit(site_url or page.url)
    path = f"{parts.scheme}://{parts.netloc}{prof.free_number_path}"

    ok, msg = False, "unknown error"
    for attempt in range(1, FREE_NUMBER_MAX_ATTEMPTS + 1):
        try:
            result = page.evaluate(_FREE_NUMBER_FETCH_JS, [path, new_phone])
        except Exception as e:
            # e.g. a post-OTP-verify redirect killing the execution context
            # mid-evaluate -- a timing race, not a real failure.
            ok, msg = False, f"request failed: {str(e)[:150]}"
        else:
            try:
                body = json.loads(result["text"])
                msg = body.get("message") or str(body)
                ok = result["status"] < 400 and not http_is_error(body)
            except (ValueError, TypeError):
                ok, msg = False, f"HTTP {result['status']}: {result['text'][:150]}"

        if ok or attempt == FREE_NUMBER_MAX_ATTEMPTS:
            break
        page.wait_for_timeout(FREE_NUMBER_RETRY_COOLDOWN_SECS * 1000)

    if not ok and FREE_NUMBER_MAX_ATTEMPTS > 1:
        msg = f"gave up after {FREE_NUMBER_MAX_ATTEMPTS} attempts: {msg}"
    return ok, new_phone, msg


def free_account_number(page, username, password, site_url=None):
    """Free up the phone number on an EXISTING account (not a fresh signup):
    log in with `username`/`password`, then swap the account's mobile number
    to a random new one via free_phone_number(), the same call the signup
    flow makes automatically right after OTP verify. Returns a result dict in
    the same shape convention as test_baccarat(): {"ok", "messages", "shot",
    "freed_phone"}. Does not write to accounts.db -- like test_baccarat(),
    this operates on an account someone already has, not one generated here."""
    result = {"ok": False, "messages": [], "shot": None, "freed_phone": None}

    outcome, msgs = login(page, username, password, site_url=site_url)
    if outcome != "ok":
        result["messages"] = msgs or [f"Login did not succeed (outcome={outcome})."]
        stamp = time.strftime("%Y%m%d-%H%M%S")
        result["shot"] = save_screenshot(page, SHOTS_DIR / f"{username}-{stamp}-login-failed.png")
        return result

    ok, new_phone, msg = free_phone_number(page, site_url=site_url)
    result["ok"] = ok
    result["messages"] = [msg]
    if ok:
        result["freed_phone"] = new_phone

    stamp = time.strftime("%Y%m%d-%H%M%S")
    result["shot"] = save_screenshot(page, SHOTS_DIR / f"{username}-{stamp}-free-number.png")
    return result


def run_free_account_number(username, password, site_url=None, proxy=None):
    """Standalone single-call entry point for freeing one EXISTING account's
    phone number: launches its own throwaway Playwright browser + context
    (same _launch_pw_browser()/parse_proxy()/maybe_bridge_proxy() pattern
    run_balance_check()/run_change_account_password() use), logs in, frees
    the number via free_account_number(), and tears everything down on every
    exit path -- same one-call convention, for phone_freer.py (or any other
    caller) that isn't already holding a page/context. Returns
    free_account_number()'s result dict ({"ok", "messages", "shot",
    "freed_phone"})."""
    result = {"ok": False, "messages": [], "shot": None, "freed_phone": None}
    try:
        pw, browser = _launch_pw_browser()
    except Exception as e:
        result["messages"] = [f"Failed to launch browser: {e}"]
        return result

    bridge_proc = None
    try:
        proxy_conf = parse_proxy(proxy) if proxy else None
        try:
            proxy_conf, bridge_proc = maybe_bridge_proxy(proxy_conf)
        except RuntimeError as e:
            result["messages"] = [f"Proxy bridge failed to start: {e}"]
            return result
        # new_site_context, not a bare new_context: winclash's WAF flat-403s
        # headless Chromium's default user agent, so a login-based feature on
        # that site never even reaches the page without the profile's UA. A
        # no-op for every site that sets no user_agent.
        context = new_site_context(browser, site_url or SITE_URL, proxy_conf=proxy_conf)
        try:
            page = context.new_page()
            result = free_account_number(page, username, password, site_url=site_url)
        finally:
            try:
                context.close()
            except Exception:
                pass
    finally:
        stop_bridge(bridge_proc)
        try:
            browser.close()
        finally:
            pw.stop()
    return result


_CHANGE_PASSWORD_FETCH_JS = """async (args) => {
    const [path, oldPassword, newPassword] = args;
    const csrf = document.querySelector('meta[name=csrf-token]').content;
    const resp = await fetch(path, {
        method: "POST",
        credentials: "same-origin",
        headers: {
            "Accept": "*/*",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest",
            "DNT": "1",
        },
        body: "oldPassword=" + encodeURIComponent(oldPassword)
            + "&newPassword=" + encodeURIComponent(newPassword)
            + "&_token=" + encodeURIComponent(csrf),
    });
    const text = await resp.text();
    return {status: resp.status, text: text};
}"""


def change_account_password(page, current_password, new_password, site_url=None):
    """Change the password on an already-logged-in account. `page` must
    already be on a logged-in session (see change_account_password_via_login()
    below, same login()-then-mutate pairing as free_phone_number()).

    The endpoint (POST /changePassword, sites/cricmatch.py's
    change_password_path) was found live via a real, user-captured HTTP
    request/response (manual request interception, 2026-07-30) -- NOT
    guessed. Confirmed end-to-end: logging in with the NEW password worked
    afterward, and the OLD password no longer did.

    Fired as a real in-page fetch() via page.evaluate(), same reasoning as
    free_phone_number(): page.context.request is a separate out-of-band HTTP
    client missing whatever Chromium's own in-page requests carry (e.g.
    sec-ch-ua/* client hints), which other endpoints on this site have been
    confirmed live to require.

    The response is its OWN shape, NOT the {"message","message_class"} shape
    most other endpoints here use -- confirmed live:
    {"status":200,"msg":"Password updated successfully"}. Note the key is
    "msg", not "message", and there is no "message_class" field, so
    http_is_error() does not apply here; success is judged by the response
    body's own "status" field (an app-level code, separate from the HTTP
    status) equalling 200.

    No retry loop (unlike free_phone_number()'s 15x45s budget) -- that width
    was earned empirically against a rate-limit specific to THAT endpoint.
    Don't add one here speculatively; only widen this if live testing
    surfaces the same kind of WAF/rate-limit behavior.

    Settle wait: confirmed live 2026-07-30 that calling this right after
    login() returns "ok" gets a false "please add phone number before
    changing the password" rejection even on an account that DOES have a
    verified number -- looks like the exact same "session isn't settled
    yet" failure mode free_phone_number() hit (see its docstring), just
    surfacing as a different wrong message here instead of a 500. Added the
    same 8s wait as a fix on that theory (not yet re-verified live against
    the same account) -- if a real number+phone account still gets this
    rejection after the wait, the theory is wrong and needs more digging,
    not a wider wait.

    Returns (ok, message)."""
    prof = profile_for(page.url)
    if not prof.supports_change_password:
        return False, f"{prof.key} does not support self-service password change."

    page.wait_for_timeout(8000)
    parts = urlsplit(site_url or page.url)
    path = f"{parts.scheme}://{parts.netloc}{prof.change_password_path}"

    try:
        result = page.evaluate(_CHANGE_PASSWORD_FETCH_JS, [path, current_password, new_password])
    except Exception as e:
        return False, f"request failed: {str(e)[:150]}"

    try:
        body = json.loads(result["text"])
    except (ValueError, TypeError):
        return False, f"HTTP {result['status']}: {result['text'][:150]}"

    msg = body.get("msg") or body.get("message") or str(body)
    ok = result["status"] < 400 and body.get("status") == 200
    return ok, msg


def change_account_password_via_login(page, username, current_password, new_password, site_url=None):
    """Change the password on an EXISTING account: log in with the CURRENT
    password, then change it via change_account_password(). Returns a result
    dict in the same shape convention as free_account_number()/test_baccarat():
    {"ok", "messages", "shot", "new_password"}. Does not write to accounts.db
    -- same "ad-hoc action on an account someone already has" lifecycle as
    free_account_number()."""
    result = {"ok": False, "messages": [], "shot": None, "new_password": None}

    outcome, msgs = login(page, username, current_password, site_url=site_url)
    if outcome != "ok":
        result["messages"] = msgs or [f"Login did not succeed (outcome={outcome})."]
        stamp = time.strftime("%Y%m%d-%H%M%S")
        result["shot"] = save_screenshot(page, SHOTS_DIR / f"{username}-{stamp}-login-failed.png")
        return result

    ok, msg = change_account_password(page, current_password, new_password, site_url=site_url)
    result["ok"] = ok
    result["messages"] = [msg]
    if ok:
        result["new_password"] = new_password

    stamp = time.strftime("%Y%m%d-%H%M%S")
    result["shot"] = save_screenshot(page, SHOTS_DIR / f"{username}-{stamp}-change-password.png")
    return result


def run_change_account_password(username, current_password, new_password, site_url=None, proxy=None):
    """Standalone single-call entry point for one account's password change:
    launches its own throwaway Playwright browser + context (same
    _launch_pw_browser()/parse_proxy()/maybe_bridge_proxy() pattern
    run_balance_check() uses), logs in, changes the password, and tears
    everything down on every exit path -- same one-call convention as
    run_balance_check(), for password_changer.py (or any other caller) that
    isn't already holding a page/context. Returns
    change_account_password_via_login()'s result dict."""
    result = {"ok": False, "messages": [], "shot": None, "new_password": None}
    try:
        pw, browser = _launch_pw_browser()
    except Exception as e:
        result["messages"] = [f"Failed to launch browser: {e}"]
        return result

    bridge_proc = None
    try:
        proxy_conf = parse_proxy(proxy) if proxy else None
        try:
            proxy_conf, bridge_proc = maybe_bridge_proxy(proxy_conf)
        except RuntimeError as e:
            result["messages"] = [f"Proxy bridge failed to start: {e}"]
            return result
        # new_site_context, not a bare new_context: winclash's WAF flat-403s
        # headless Chromium's default user agent, so a login-based feature on
        # that site never even reaches the page without the profile's UA. A
        # no-op for every site that sets no user_agent.
        context = new_site_context(browser, site_url or SITE_URL, proxy_conf=proxy_conf)
        try:
            page = context.new_page()
            result = change_account_password_via_login(
                page, username, current_password, new_password, site_url=site_url)
        finally:
            try:
                context.close()
            except Exception:
                pass
    finally:
        stop_bridge(bridge_proc)
        try:
            browser.close()
        finally:
            pw.stop()
    return result


WALLET_BALANCE_TIMEOUT_SECS = 30
WALLET_BALANCE_POLL_MS = 1000


def read_wallet_balance(page, timeout_secs=WALLET_BALANCE_TIMEOUT_SECS):
    """Scrape the site's OWN wallet balance from the logged-in header
    (cricmatch247's "My Wallet") -- NOT the in-game Evolution balance
    read_game_balance() reads from a live casino table's own frame; this
    reads the site chrome around it instead.

    VERIFIED LIVE 2026-07-30 against a real account (ali789, via
    inspect_wallet.py): the figure lives in `span.total_balance` (mirrored
    in `span.wallet_balance` under "Available" -- same number, either works),
    `sites/cricmatch.py`'s sel["wallet_balance"]. Two non-obvious things
    found live, both handled here:
    1. Both spans are EMPTY in the raw HTML at page-load -- the site fills
       them in later via its own onload getBalance() call. In one real test
       this took ~20s on a residential proxy, so this polls for up to
       `timeout_secs` rather than reading once immediately after login.
    2. A real navigation happens shortly after login (same as the redirect
       free_phone_number() has to tolerate -- see CLAUDE.md), which can kill
       the execution context mid-poll ("Execution context was destroyed").
       That's treated as "try again next tick," not a hard failure.

    Returns a float (rupees, may include paise, e.g. "₹ 1,484.68" -> 1484.68)
    or None if it never populates within the timeout -- a caller must treat
    None as "selector/timing needs revisiting," never as "balance is 0.\""""
    prof = profile_for(page.url)
    sel = prof.sel.get("wallet_balance")
    if not sel:
        return None
    deadline = time.time() + timeout_secs
    while time.time() < deadline:
        try:
            vals = page.eval_on_selector_all(sel, "els => els.map(e => e.innerText)")
        except Exception:
            page.wait_for_timeout(WALLET_BALANCE_POLL_MS)
            continue
        for v in vals:
            v = (v or "").strip()
            if not v:
                continue
            m = re.search(r"[\d,]+(?:\.\d+)?", v)
            if m:
                try:
                    return float(m.group(0).replace(",", ""))
                except ValueError:
                    pass
        page.wait_for_timeout(WALLET_BALANCE_POLL_MS)
    return None


def check_account_balance(page, username, password, site_url=None):
    """Log into an EXISTING account and read its site wallet balance. Returns
    a result dict in the same shape convention as free_account_number()/
    test_baccarat(): {"ok", "balance", "messages", "shot"}. Read-only -- does
    not place a bet or change anything, and does not write to accounts.db
    (like free_account_number(), this operates on an account someone already
    has, not one generated here)."""
    result = {"ok": False, "balance": None, "messages": [], "shot": None,
              "page": page}

    # Same preamble as _open_table_for, and for the same reason: on a site
    # whose every page load can be met by an AWS WAF interstitial (winclash),
    # login() would otherwise spend its whole timeout hunting a LOGIN button
    # on a "Human Verification" page. Clearing the wall can hand back a page
    # in a BRAND-NEW context (a solved token only works in a fresh one), so
    # the page is published in result["page"] -- the same convention
    # signup_once() uses -- and run_balance_check closes whichever context the
    # page ended up in.
    prof = profile_for(site_url or SITE_URL)
    if prof.waf_on_navigation:
        try:
            page.goto(login_url_for(site_url), wait_until="domcontentloaded",
                      timeout=60000)
        except PWError as e:
            result["messages"] = [f"Could not load the site: {str(e)[:150]}"]
            return result
        page, waf_ok, waf_msg = ensure_waf_cleared(page, site_url or SITE_URL)
        result["page"] = page
        if not waf_ok:
            result["messages"] = [f"Blocked before login: {waf_msg}"]
            return result

    outcome, msgs = login(page, username, password, site_url=site_url,
                          already_loaded=prof.waf_on_navigation)
    if outcome == "waf":
        # The WAF refused the login CALL, not the credentials. Only a fresh
        # token in a FRESH context fixes that, and it is worth exactly one
        # retry -- if the wall survives a solve the exit IP is flagged and
        # more attempts only spend CapSolver credit. Same rule as
        # _open_table_for.
        page, waf_ok, waf_msg = ensure_waf_cleared(page, site_url or SITE_URL,
                                                   force=True)
        result["page"] = page
        if waf_ok:
            try:
                page.goto(login_url_for(site_url),
                          wait_until="domcontentloaded", timeout=60000)
            except PWError:
                pass
            outcome, msgs = login(page, username, password, site_url=site_url,
                                  already_loaded=True)
        else:
            msgs = [f"{msgs[0] if msgs else 'AWS WAF blocked the login'}; "
                    f"clearing it failed too: {waf_msg}"]
    if outcome != "ok":
        result["messages"] = msgs or [f"Login did not succeed (outcome={outcome})."]
        stamp = time.strftime("%Y%m%d-%H%M%S")
        result["shot"] = save_screenshot(page, SHOTS_DIR / f"{username}-{stamp}-balance-login-failed.png")
        return result

    balance = read_wallet_balance(page)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    result["shot"] = save_screenshot(page, SHOTS_DIR / f"{username}-{stamp}-balance.png")
    if balance is None:
        result["messages"] = ["Logged in, but couldn't find the wallet balance on the page "
                               "(selector not yet verified live -- see inspect_wallet.py)."]
        return result

    result["ok"] = True
    result["balance"] = balance
    result["messages"] = [f"Balance: ₹{balance}"]
    return result


def run_balance_check(username, password, site_url=None, proxy=None):
    """Standalone single-call entry point for one account's balance check:
    launches its own throwaway Playwright browser + context (same
    _launch_pw_browser()/parse_proxy()/maybe_bridge_proxy() pattern
    run_paired_hedge() uses per side), logs in, reads the balance, and tears
    everything down on every exit path -- the one-call convention
    balance_checker.py (or any other caller) uses, mirroring how
    run_paired_hedge() is one call for a whole hedge run rather than exposing
    browser plumbing to its callers. Returns check_account_balance()'s result
    dict."""
    result = {"ok": False, "balance": None, "messages": [], "shot": None}
    try:
        pw, browser = _launch_pw_browser()
    except Exception as e:
        result["messages"] = [f"Failed to launch browser: {e}"]
        return result

    bridge_proc = None
    try:
        proxy_conf = parse_proxy(proxy) if proxy else None
        try:
            proxy_conf, bridge_proc = maybe_bridge_proxy(proxy_conf)
        except RuntimeError as e:
            result["messages"] = [f"Proxy bridge failed to start: {e}"]
            return result
        # new_site_context, not a bare new_context: winclash's WAF flat-403s
        # headless Chromium's default user agent, so a login-based feature on
        # that site never even reaches the page without the profile's UA. A
        # no-op for every site that sets no user_agent.
        context = new_site_context(browser, site_url or SITE_URL, proxy_conf=proxy_conf)
        try:
            page = context.new_page()
            result = check_account_balance(page, username, password, site_url=site_url)
        finally:
            # Clearing a WAF wall can move the page into a brand-new context
            # and close the original, so close the one the page actually ended
            # up in -- otherwise that replacement leaks until browser.close().
            for ctx in {context, getattr(result.get("page"), "context", None)}:
                try:
                    if ctx is not None:
                        ctx.close()
                except Exception:
                    pass
    finally:
        stop_bridge(bridge_proc)
        try:
            browser.close()
        finally:
            pw.stop()
    # Page and context are closed by now, and callers of this one-call entry
    # point (balance_checker.py, the bot) persist the result dict -- so don't
    # hand them a dead Playwright handle to trip over.
    result.pop("page", None)
    return result


# ---------------------------------------------------------------------------
# HTTP-fast balance check (no browser at all) -- same idea as HTTP-fast
# signup above, discovered the same way: capture a real Playwright login's
# network traffic (probe_login_balance.py), then confirm live with a bare
# requests.Session replay. For sites with supports_http_login=True this
# replaces a ~20-30s browser login + poll-for-balance with two plain HTTP
# POSTs -- roughly two orders of magnitude faster, and light enough that many
# accounts can be checked concurrently without launching a browser per
# account. Falls back to run_balance_check() (the Playwright path) for any
# site that doesn't set the flag.
# ---------------------------------------------------------------------------

def http_login_call(session, csrf_token, username, password, site_url):
    """POST the login endpoint. Returns the parsed JSON body (or a synthetic
    error dict if the response isn't JSON)."""
    prof = profile_for(site_url)
    parts = urlsplit(site_url)
    login_url = f"{parts.scheme}://{parts.netloc}{prof.http_login_path}"
    data = {"username": username, "password": password, "remember_me": "1", "_token": csrf_token}
    headers = {"X-Requested-With": "XMLHttpRequest", "X-CSRF-TOKEN": csrf_token, "Referer": site_url,
               "Origin": _http_fast_origin(site_url)}
    resp = session.post(login_url, data=data, headers=headers, timeout=20)
    try:
        return resp.json()
    except ValueError:
        return {"status": None, "message": f"Non-JSON response (HTTP {resp.status_code}): {resp.text[:200]}"}


def http_get_balance(session, csrf_token, site_url):
    """POST the balance endpoint on an authenticated session. Returns the
    parsed JSON body (or a synthetic error dict if the response isn't JSON)."""
    prof = profile_for(site_url)
    parts = urlsplit(site_url)
    url = f"{parts.scheme}://{parts.netloc}{prof.http_balance_path}"
    headers = {"X-Requested-With": "XMLHttpRequest", "X-CSRF-TOKEN": csrf_token, "Referer": site_url,
               "Origin": _http_fast_origin(site_url)}
    resp = session.post(url, data={"_token": csrf_token}, headers=headers, timeout=20)
    try:
        return resp.json()
    except ValueError:
        return {"status": None, "message": f"Non-JSON response (HTTP {resp.status_code}): {resp.text[:200]}"}


def http_check_account_balance(username, password, site_url=None, proxy=None):
    """HTTP-only counterpart to run_balance_check(): no browser launch, no
    page render, no polling for a delayed getBalance() call -- just the two
    real POSTs the site's own JS makes (login, then getBalance). Same result
    shape ({"ok","balance","messages","shot"}), "shot" always None since
    there's no page to screenshot. Verified live 2026-07-30 against a real
    account (ali789) -- balance matched the browser-path reading exactly."""
    url = site_url or SITE_URL
    prof = profile_for(url)
    # infra_block distinguishes "the edge/WAF blocked the request before the
    # app ever saw it" (bare 403/non-JSON -- http_login_call/http_get_balance
    # return status=None for exactly this case, since a real app response is
    # always JSON with a real status) from a genuine application result
    # (wrong password, "Account has been Blocked", success, etc, all real
    # JSON). Callers (balance_checker.py) use this to auto-retry instead of
    # permanently recording a result for what was never actually checked --
    # confirmed live 2026-07-30/31 that a tripped block returns 403 on every
    # attempt for ~20 minutes regardless of pacing, so treating each one as a
    # terminal per-account failure was both wrong and what made ~2k-account
    # sheets grind to a halt (blocked rows got stuck needing manual retry).
    result = {"ok": False, "balance": None, "messages": [], "shot": None, "infra_block": False}

    if not prof.supports_http_login:
        result["messages"] = [f"{prof.key} does not support HTTP-fast balance checks."]
        return result

    try:
        session = http_session_for(proxy)
    except ValueError as e:
        result["messages"] = [f"Bad proxy value: {e}"]
        return result

    try:
        csrf = http_fetch_csrf(session, url, proxy)
    except (requests.RequestException, RuntimeError) as e:
        result["messages"] = [f"Could not load the site (check the URL/proxy?): {str(e)[:200]}"]
        result["infra_block"] = True
        return result

    login_resp = http_login_call(session, csrf, username, password, url)
    if login_resp.get("status") != 200:
        result["messages"] = [login_resp.get("message") or f"Login failed: {login_resp}"]
        result["infra_block"] = login_resp.get("status") is None
        return result

    bal_resp = http_get_balance(session, csrf, url)
    if bal_resp.get("status") != 200:
        result["messages"] = [bal_resp.get("message") or f"Could not read balance: {bal_resp}"]
        result["infra_block"] = bal_resp.get("status") is None
        return result

    bal = (bal_resp.get("balance") or {})
    wallet = bal.get("wallet")
    if wallet is None:
        raw = bal.get("totalBalance") or bal.get("balance")
        try:
            wallet = float(str(raw).replace(",", ""))
        except (TypeError, ValueError):
            wallet = None
    if wallet is None:
        result["messages"] = [f"Logged in, but couldn't parse a balance out of the response: {bal_resp}"]
        return result

    result["ok"] = True
    result["balance"] = float(wallet)
    result["messages"] = [f"Balance: ₹{wallet}"]
    return result


def http_change_account_password(username, current_password, new_password,
                                 site_url=None, proxy=None):
    """HTTP-only counterpart to run_change_account_password(): no browser, just
    the two POSTs the site's own JS makes (login, then changePassword). Same
    result shape ({"ok","messages","shot","new_password"}), "shot" always None
    since there is no page to screenshot.

    Only used where the profile sets supports_http_change_password. On
    cricmatch the change-password call is deliberately fired as an in-page
    fetch() because an out-of-band client misbehaves there, so this must never
    be assumed to work from supports_change_password alone.

    Response convention (confirmed live on starexch 2026-08-19, and the same
    shape cricmatch documents): {"status":200,"msg":"..."} on success,
    {"status":201,"msg":"Please enter the valid current password"} when the
    current password is wrong -- the key is "msg", there is no
    "message_class", and success is status == 200 specifically, so
    http_is_error() does NOT apply here.

    Like http_check_account_balance(), sets result["infra_block"] when the
    edge answered instead of the app (a bare 403 / any non-JSON body), so a
    caller can retry that rather than recording it as a real per-account
    failure."""
    url = site_url or SITE_URL
    prof = profile_for(url)
    result = {"ok": False, "messages": [], "shot": None, "new_password": None,
              "infra_block": False}

    if not (prof.supports_change_password and prof.supports_http_change_password):
        result["messages"] = [
            f"{prof.key} does not support HTTP-fast password changes."]
        return result

    try:
        session = http_session_for(proxy)
    except ValueError as e:
        result["messages"] = [f"Bad proxy value: {e}"]
        return result

    try:
        csrf = http_fetch_csrf(session, url, proxy)
    except (requests.RequestException, RuntimeError) as e:
        result["messages"] = [f"Could not load the site (check the URL/proxy?): {str(e)[:200]}"]
        result["infra_block"] = True
        return result

    login_resp = http_login_call(session, csrf, username, current_password, url)
    if login_resp.get("status") != 200:
        result["messages"] = [login_resp.get("message") or f"Login failed: {login_resp}"]
        result["infra_block"] = login_resp.get("status") is None
        return result

    parts = urlsplit(url)
    change_url = f"{parts.scheme}://{parts.netloc}{prof.change_password_path}"
    headers = {"X-Requested-With": "XMLHttpRequest", "X-CSRF-TOKEN": csrf,
               "Referer": url, "Origin": _http_fast_origin(url)}
    try:
        resp = session.post(change_url,
                            data={"oldPassword": current_password,
                                  "newPassword": new_password,
                                  "_token": csrf},
                            headers=headers, timeout=20)
    except requests.RequestException as e:
        result["messages"] = [f"Change-password request failed: {str(e)[:200]}"]
        result["infra_block"] = True
        return result

    try:
        body = resp.json()
    except ValueError:
        result["messages"] = [
            f"Non-JSON response (HTTP {resp.status_code}): {resp.text[:200]}"]
        result["infra_block"] = True
        return result

    msg = body.get("msg") or body.get("message") or str(body)
    if body.get("status") == 200:
        result["ok"] = True
        result["new_password"] = new_password
        result["messages"] = [msg]
    else:
        result["messages"] = [msg]
    return result


def signup_once(page, acct, submit=True, interactive=False, site_url=None, proxy=None,
                free_number=False, proxy_conf=None):
    """Run one signup attempt. Returns a result dict.

    If the site rejects the phone number as already registered and
    `interactive` is True, prompts for a different phone number and retries
    (up to 5 times) instead of failing outright. `proxy` (raw string) is only
    used to route the AWS WAF CAPTCHA solve through the same egress IP.

    `free_number=True` swaps the account onto a random throwaway phone number
    right after a successful OTP verify (see free_phone_number()), so the
    real phone used for this signup is freed up for the next one."""
    result = {"account": acct.get("username", "?"), "ok": None, "messages": [], "shot": None}

    # A token solved for an earlier signup lets this context be served the
    # real site on its first GET, which also means the register page can be
    # opened directly instead of by way of the homepage.
    seeded = seed_waf_token(page.context, site_url, proxy)
    page.goto(signup_entry_url(site_url, seeded), wait_until="domcontentloaded",
              timeout=60000)
    post_load_settle(page, site_url)

    # Some sites (winclash) put an AWS WAF wall in front of the homepage
    # itself, so there is nothing to click until it's cleared. This is a
    # no-op on every site that doesn't serve one. It can hand back a fresh
    # page in a fresh context, so rebind `page` and record it for the caller
    # to close -- same contract as submit_register() below.
    page, waf_ok, waf_msg = ensure_waf_cleared(page, site_url, proxy=proxy,
                                               proxy_conf=proxy_conf)
    result["page"] = page
    if not waf_ok:
        result["ok"] = False
        result["messages"] = [waf_msg]
        return result
    if waf_msg:
        result["messages"].append(waf_msg)

    form_ok, page, form_msg = open_signup_form(page, site_url, proxy=proxy,
                                               proxy_conf=proxy_conf)
    result["page"] = page
    if not form_ok:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        result["ok"] = False
        result["messages"] = [form_msg]
        result["shot"] = save_screenshot(
            page, SHOTS_DIR / f"{acct.get('username', 'unknown')}-{stamp}-no-modal.png")
        return result
    if form_msg:
        result["messages"].append(form_msg)

    # Type (not fill) so the site's live validation/keyup handlers fire; blur
    # each field afterward to trigger any on-blur checks. Also ensures the
    # "I'm over 18 + T&C" box is checked.
    fill_register_form(page, acct)

    stamp = time.strftime("%Y%m%d-%H%M%S")
    filled_shot = SHOTS_DIR / f"{acct['username']}-{stamp}-filled.png"
    save_screenshot(page, filled_shot)

    if not submit:
        result["ok"] = True
        # Append, don't replace: anything already in here (e.g. "AWS WAF
        # cleared with a CapSolver token") is exactly what a --no-submit dry
        # run is for -- discarding it hid the one step worth reporting.
        result["messages"].append("--no-submit: form filled but not submitted.")
        result["shot"] = save_screenshot(page, filled_shot)
        return result

    # submit_register may swap in a fresh page/context (see its docstring) if
    # it had to route around an AWS WAF CAPTCHA -- reassign the local `page`
    # so every use below (screenshots, phone-taken retry, enter_otp) targets
    # whichever page is actually live, and stash it in `result` so the caller
    # knows which context to close (the original may already be closed).
    outcome, msgs, _, page = submit_register(page, acct, site_url, proxy=proxy,
                                             proxy_conf=proxy_conf)
    result["page"] = page

    attempts = 0
    while outcome == "phone_taken" and interactive and attempts < 5:
        attempts += 1
        # check_phone_taken() only answers on sites with a dedicated element
        # for it; sites classified by toast TEXT (phone_taken_texts) return
        # None there, and their real wording is in `msgs`. Falling back
        # through both is what stops the prompt reading literally "None Try a
        # different phone number."
        phone_err = check_phone_taken(page) or "; ".join(msgs) or "That number is taken."
        print(f"\n{phone_err} Try a different phone number.")
        acct["phone"] = prompt_phone(site_url)
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
    result["shot"] = save_screenshot(page, result_shot)

    if outcome == "phone_taken":
        # Same fallback order as above, so a text-matched site records what
        # the site actually said instead of a generic stand-in.
        phone_err = check_phone_taken(page) or "; ".join(msgs)
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
    result = enter_otp(page, acct, result)

    # Only attempt the phone swap where the site actually has that endpoint.
    # Checking the profile HERE rather than only inside free_phone_number()
    # keeps an inapplicable site from reporting "Free-number FAILED: <site>
    # does not support freeing the signup phone number" on every successful
    # signup -- nothing failed, the step simply doesn't exist there.
    if result.get("ok") and free_number and profile_for(page.url).supports_free_number:
        ok, new_phone, msg = free_phone_number(page, site_url or SITE_URL)
        result["freed_phone"] = new_phone if ok else None
        result["messages"].append(f"Free-number: {msg}" if ok else f"Free-number FAILED: {msg}")

    return result


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

# Matches a real captured browser session (getBalance call intercepted
# 2026-07-30 via a mobile HTTP-interceptor tool) rather than the earlier
# generic Windows/Chrome-124 string -- a plain `requests` call has no TLS/
# fingerprint to hide behind, so at least matching the header *set* a real
# Chrome sends (client hints included) makes automated traffic look less
# obviously scripted to a WAF than a bare User-Agent with nothing else.
_HTTP_FAST_USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                         "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36")
_HTTP_FAST_SEC_CH_UA = '"Chromium";v="127", "Not)A;Brand";v="99", "Google Chrome";v="127"'


def _http_fast_browser_headers():
    """Static (non-request-specific) headers a real Chrome/macOS session
    sends on every request -- see _HTTP_FAST_USER_AGENT's comment."""
    return {
        "User-Agent": _HTTP_FAST_USER_AGENT,
        "Accept": "*/*",
        "DNT": "1",
        "sec-ch-ua": _HTTP_FAST_SEC_CH_UA,
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"macOS"',
    }


def _http_fast_origin(site_url):
    """scheme://host with no path/query, for the Origin header a real
    browser sends on same-origin XHR POSTs."""
    parts = urlsplit(site_url)
    return f"{parts.scheme}://{parts.netloc}"


def http_session_for(proxy_str):
    """Build a requests.Session for the given proxy string (parse_proxy()'s
    format). Unlike Chromium, requests can authenticate to SOCKS5 proxies
    directly (via PySocks) -- the pproxy bridge in maybe_bridge_proxy() works
    around a Chromium-specific limitation that doesn't apply here."""
    session = requests.Session()
    session.headers.update(_http_fast_browser_headers())
    if proxy_str:
        conf = parse_proxy(proxy_str)
        scheme, hostport = conf["server"].split("://", 1)
        user, pw = conf.get("username"), conf.get("password")
        auth = f"{user}:{pw}@" if user and pw else ""
        proxy_url = f"{scheme}://{auth}{hostport}"
        session.proxies = {"http": proxy_url, "https": proxy_url}
    return session


# Headers a real Chrome sends when NAVIGATING to a page, as opposed to the
# XHR-shaped set in _http_fast_browser_headers(). This distinction is not
# cosmetic: AWS WAF answers a challenged XHR-shaped GET with a bare HTTP 202
# and an EMPTY BODY -- nothing to read challenge parameters out of, so the
# wall looks unsolvable -- while the same GET with navigation headers is
# served the real interstitial, gokuProps and all. Confirmed live on winclash
# 2026-09-01: 202 + 0 bytes with Accept: */*, 202 + 2407 bytes carrying
# gokuProps with the headers below.
_HTTP_NAV_HEADERS = {
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "image/avif,image/webp,*/*;q=0.8"),
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}


def http_apply_waf_token(session, site_url, token):
    """Put a solved aws-waf-token into a requests.Session's cookie jar.

    Note the contrast with the browser path, where a token only ever works in
    a BRAND-NEW context (see restart_with_waf_token()). Over plain HTTP there
    is no such rule -- verified live 2026-09-01 on winclash, where the same
    token cleared the wall both in the session that had been challenged and in
    a freshly built one -- so nothing needs rebuilding here."""
    host = urlsplit(site_url or SITE_URL).hostname
    session.cookies.set("aws-waf-token", token, domain=host, path="/")


def http_get_page(session, url, site_url=None, proxy=None):
    """GET a page over plain HTTP, clearing an AWS WAF wall on the way if one
    turns up. Returns the requests Response.

    winclash's WAF is behavioural: a clean IP is served normally, and after a
    handful of rapid requests the same GET starts coming back with an
    `x-amzn-waf-action` header. The soft "challenge" action is what a browser
    clears for free by running challenge.js, which `requests` cannot do -- but
    CapSolver mints the identical token from the challenge parameters, and
    that token works here.

    Tokens come from and go back into the SAME cache the browser path uses
    (cached_waf_token/cache_waf_token, keyed by host + egress), so a solve
    paid for by a browser signup clears the wall for an HTTP one and vice
    versa."""
    site = site_url or url
    # A site with no wall in front of it is fetched EXACTLY as it always was:
    # same plain GET, same headers, same timeout. Nothing about cricmatch's or
    # starexch's HTTP paths changes because winclash needed this.
    if not profile_for(site).waf_on_navigation:
        return session.get(url, timeout=20)

    token = cached_waf_token(site, proxy)
    if token:
        http_apply_waf_token(session, site, token)

    resp = session.get(url, headers=_HTTP_NAV_HEADERS, timeout=25)
    if not resp.headers.get("x-amzn-waf-action"):
        return resp

    # The cached token (if there was one) has just been disproved -- same
    # reasoning as ensure_waf_cleared()'s: don't spend a second request
    # re-presenting it.
    if token:
        forget_waf_token(site, proxy)
    if not capsolver_key():
        raise RuntimeError("AWS WAF is challenging this request and "
                           "CAPSOLVER_API_KEY is not set, so it can't be solved")

    challenge = parse_aws_waf_challenge(resp.text)
    if not challenge:
        raise RuntimeError(
            f"AWS WAF answered HTTP {resp.status_code} "
            f"({resp.headers.get('x-amzn-waf-action')}) but its challenge "
            f"parameters could not be read from the {len(resp.text)}-byte body")
    token = solve_aws_waf_token(site, challenge, proxy=proxy)
    http_apply_waf_token(session, site, token)
    cache_waf_token(site, token, proxy)

    resp = session.get(url, headers=_HTTP_NAV_HEADERS, timeout=25)
    if resp.headers.get("x-amzn-waf-action"):
        forget_waf_token(site, proxy)
        raise RuntimeError("AWS WAF is still challenging after presenting a "
                           "freshly solved token")
    return resp


def http_csrf_url(site_url):
    """The page whose csrf token the register POST must carry. cricmatch's
    register form is on the site root; winclash's is its own page, and the
    _token belongs to that page."""
    prof = profile_for(site_url)
    path = prof.http_csrf_path or (prof.register_path if prof.register_trigger == "page_url" else "")
    return urljoin(site_url, path) if path else site_url


def http_fetch_csrf(session, site_url, proxy=None):
    """GET the page carrying the register form and pull the Laravel csrf-token
    meta tag out of the response (also seeds the session's cookies --
    laravel_session/jeet_session, XSRF-TOKEN, AWSALB* -- for the register calls
    that follow). Clears an AWS WAF wall if the site serves one."""
    resp = http_get_page(session, http_csrf_url(site_url), site_url, proxy)
    resp.raise_for_status()
    m = re.search(r'<meta name="csrf-token" content="([^"]+)"', resp.text)
    if not m:
        raise RuntimeError("csrf-token meta tag not found on the signup page "
                           "response (site markup may have changed).")
    return m.group(1)


def http_register_call(session, csrf_token, acct, site_url, otp=""):
    """POST the register endpoint. `otp=""` triggers the SMS; a follow-up call
    with the real code verifies it -- same endpoint both times, confirmed live.
    Returns the parsed JSON body (or a synthetic error dict if the response
    isn't JSON, e.g. an unexpected block page)."""
    prof = profile_for(site_url)
    parts = urlsplit(site_url)
    register_url = f"{parts.scheme}://{parts.netloc}{prof.http_register_path}"
    # Field names come from the profile: the two sites' forms carry the same
    # six values under different names (username/user_name,
    # phone/mobile_number), so this stays one code path.
    names = prof.http_register_fields
    data = {
        names["username"]: acct["username"],
        names["email"]: acct["email"],
        names["password"]: acct["password"],
        names["phone"]: str(acct["phone"]),
        names["otp"]: otp,
        names["token"]: csrf_token,
    }
    if prof.http_confirm_password_field:
        data[prof.http_confirm_password_field] = acct["password"]
    headers = {
        "X-Requested-With": "XMLHttpRequest",
        "X-CSRF-TOKEN": csrf_token,
        "Referer": http_csrf_url(site_url),
        "Origin": _http_fast_origin(site_url),
    }
    resp = session.post(register_url, data=data, headers=headers, timeout=20)
    return _http_json_or_error(resp)


def _http_json_or_error(resp):
    """Parse a JSON response, turning anything else -- an edge block page, an
    empty WAF 202 -- into the same error-shaped dict the callers already read,
    so a block is reported rather than raising a JSONDecodeError."""
    try:
        return resp.json()
    except ValueError:
        waf = resp.headers.get("x-amzn-waf-action")
        detail = f" BLOCKED by AWS WAF (x-amzn-waf-action: {waf})" if waf else ""
        return {"status": None, "message_class": "danger",
                "waf_action": waf,
                "message": f"Non-JSON response (HTTP {resp.status_code}){detail}: "
                           f"{resp.text[:200]}"}


def http_confirm_signup_otp(session, csrf_token, otp, phone, site_url):
    """winclash's extra OTP step: the code is checked by its own endpoint
    BEFORE the register POST is repeated with it.

    Read out of the site's own #confirmSignupOtpBtn_ handler, which POSTs
    {otp, phone} to /api2/v2/confirmSignupOtp with an X-CSRF-Token header and,
    on statusCode 251, literally re-clicks #signUpButton -- i.e. re-sends the
    whole register form with the code in it. `phone` is the number the
    register call ECHOED BACK (data.phone, which the page renders into
    .signup_verify_number and reads straight back out), not the one we typed,
    so whatever normalisation the server applied is preserved.

    Returns the parsed JSON."""
    prof = profile_for(site_url)
    parts = urlsplit(site_url)
    url = f"{parts.scheme}://{parts.netloc}{prof.http_confirm_otp_path}"
    headers = {
        "X-Requested-With": "XMLHttpRequest",
        "X-CSRF-Token": csrf_token,
        "Referer": http_csrf_url(site_url),
        "Origin": _http_fast_origin(site_url),
    }
    resp = session.post(url, data={"otp": otp, "phone": phone},
                        headers=headers, timeout=20)
    return _http_json_or_error(resp)


def http_free_phone_number(session, csrf_token, site_url):
    """HTTP-fast counterpart to free_phone_number(): the `session` already
    carries the just-registered account's auth cookies (the same session used
    for both http_register_call() calls), and `csrf_token` is the same one
    used there -- confirmed live elsewhere in this codebase that this token
    stays valid across multiple calls in one session, so no fresh GET is
    needed here. POSTs a fresh random phone number to /send_otp_touser (the
    real endpoint -- see free_phone_number()'s docstring for how the earlier
    "/send_otp" guess was found wrong, and how this one was confirmed right
    live), which overwrites the account's registered mobile with no further
    OTP step. Returns (ok, new_phone, message).

    HONEST CAVEAT, confirmed live 2026-07-22 investigating the BROWSER path:
    a real, working call to this endpoint needed cookies (`domain_switch`,
    `screenwidth`, and Laravel-encrypted `username`/`password` cookies) that
    only showed up after a real LOGIN in a real browser -- not merely after
    register+OTP-verify over plain `requests`. This --fast path has NEVER
    been confirmed live to actually work end-to-end (unlike free_phone_number(),
    which has real before/after proof the mobile number changed) -- it may
    well hit the same generic 500 the browser path did before that cookie set
    existed. Treat a "Free-number FAILED: HTTP 500" here as an open question,
    not a code bug, until someone runs a real --fast signup with
    free_number=True and checks.

    Retries up to FREE_NUMBER_MAX_ATTEMPTS times (waiting
    FREE_NUMBER_RETRY_COOLDOWN_SECS between each), same as
    free_phone_number() -- see its docstring for why a single attempt isn't
    enough (a real run hit phone_taken on the next signup after one
    free-number call failed and wasn't retried)."""
    prof = profile_for(site_url)
    if not prof.supports_free_number:
        return False, None, f"{prof.key} does not support freeing the signup phone number."

    new_phone = gen_free_phone()
    parts = urlsplit(site_url)
    url = f"{parts.scheme}://{parts.netloc}{prof.free_number_path}"
    data = {"_token": csrf_token, "phone": new_phone}
    headers = {
        "Accept": "*/*",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "X-Requested-With": "XMLHttpRequest",
        "DNT": "1",
        "Origin": f"{parts.scheme}://{parts.netloc}",
        "Referer": site_url,
    }

    ok, msg = False, "unknown error"
    for attempt in range(1, FREE_NUMBER_MAX_ATTEMPTS + 1):
        try:
            resp = session.post(url, data=data, headers=headers, timeout=20)
        except requests.RequestException as e:
            ok, msg = False, f"free-number request failed: {str(e)[:150]}"
        else:
            try:
                body = resp.json()
                msg = body.get("message") or str(body)
                ok = resp.ok and not http_is_error(body)
            except ValueError:
                ok, msg = False, f"HTTP {resp.status_code}: {resp.text[:150]}"

        if ok or attempt == FREE_NUMBER_MAX_ATTEMPTS:
            break
        time.sleep(FREE_NUMBER_RETRY_COOLDOWN_SECS)

    if not ok and FREE_NUMBER_MAX_ATTEMPTS > 1:
        msg = f"gave up after {FREE_NUMBER_MAX_ATTEMPTS} attempts: {msg}"
    return ok, new_phone, msg


def _http_status_of(resp_json):
    """The site's own status value, as a string. Sites disagree on both the
    key (`status` vs `statusCode`) and the type (0 vs "301"), so normalise
    once here rather than at four call sites."""
    for key in ("status", "statusCode"):
        if key in resp_json and resp_json[key] is not None:
            return str(resp_json[key])
    return ""


def http_message_of(resp_json):
    """The human-readable text out of a register/OTP response. cricmatch calls
    the key `message`, winclash calls it `msg`."""
    for key in ("message", "msg"):
        val = resp_json.get(key)
        if val:
            return str(val)
    return ""


def http_is_error(resp_json, site_url=None):
    """Did this register/OTP call fail?

    Two conventions, and a site declares which it uses by whether it lists any
    success statuses (http_status_otp_sent / http_status_registered):

    * cricmatch: no statuses listed -> judge by message_class, exactly as
      before, so its behaviour is untouched.
    * winclash: statuses listed -> judge by them. Its `status` is 205 for "OTP
      sent" and 1 for "registered", while a rejection is 0 (or a `statusCode`
      of "301") with the reason in `msg`. Its rejections carry NO
      message_class at all, so the old check would have called every one of
      them a success."""
    prof = profile_for(site_url) if site_url else None
    ok_statuses = (prof.http_status_otp_sent + prof.http_status_registered) if prof else []
    if ok_statuses:
        if resp_json.get("waf_action") or _http_status_of(resp_json) == "":
            return True
        return _http_status_of(resp_json) not in [str(x) for x in ok_statuses]
    return (resp_json.get("message_class") or "").lower() in ("danger", "error")


def http_is_phone_taken(resp_json):
    """Best-effort text match on the JSON error message. NOT yet verified live
    against a genuine taken-phone response (only reachable with a number that
    already completed a real registration) -- modeled on the DOM error's known
    wording ("The mobile number has already been taken.", see
    check_phone_taken()). If this misclassifies, the raw message still reaches
    result["messages"] via the generic-error fallback, so nothing is hidden."""
    msg = http_message_of(resp_json).lower()
    if "taken" in msg and ("mobile" in msg or "phone" in msg):
        return True
    # winclash words it "The mobile number is already in use." -- matched on
    # the phone-specific phrase, never a bare "already in use", for the same
    # reason its phone_taken_texts is: a taken EMAIL worded that way would
    # otherwise send the continuous loop rotating to a new number for a fault
    # a new number cannot fix.
    return "mobile number is already in use" in msg


def http_verify_signup_otp(session, csrf, acct, site_url, otp, register_json):
    """Redeem the SMS code, whichever way this site does it. Returns the JSON
    of whichever call decides the outcome, so the caller judges it with one
    http_is_error().

    "single_post" (cricmatch): the register POST is simply repeated with the
    code in it.

    "confirm_otp" (winclash): the code goes to /api2/v2/confirmSignupOtp
    first, and only if that answers with a go-ahead status is the register
    POST repeated. This mirrors the site's own handler exactly -- it re-clicks
    #signUpButton on statusCode 251 -- and the order matters: posting the
    register form with a code the confirm step has not blessed is not what the
    site does, and was never observed to work.

    The phone sent to the confirm step is the one the register call ECHOED
    BACK, because that is what the page uses (it renders data.phone into
    .signup_verify_number and reads it straight back out), so any
    normalisation the server applied is preserved."""
    prof = profile_for(site_url)
    if prof.http_otp_flow != "confirm_otp":
        return http_register_call(session, csrf, acct, site_url, otp=otp)

    phone = str(register_json.get("phone") or acct["phone"])
    confirmed = http_confirm_signup_otp(session, csrf, otp, phone, site_url)
    if http_is_error(confirmed, site_url):
        return confirmed
    return http_register_call(session, csrf, acct, site_url, otp=otp)


def http_signup_once(acct, submit=True, interactive=False, site_url=None, proxy=None,
                     free_number=False):
    """HTTP-only signup for sites with supports_http_fast=True. Same result
    shape as signup_once() ({"account","ok","messages","shot"}) so callers can
    treat both interchangeably, except "shot" is always None -- no browser, no
    screenshot to take.

    `free_number=True` swaps the account onto a random throwaway phone number
    right after a successful OTP verify (see http_free_phone_number()), so the
    real phone used for this signup is freed up for the next one."""
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
        csrf = http_fetch_csrf(session, url, proxy)
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

        if not http_is_error(resp_json, url):
            break  # OTP triggered

        if http_is_phone_taken(resp_json) and interactive and attempts < 5:
            attempts += 1
            print(f"\n{http_message_of(resp_json)} Try a different phone number.")
            acct["phone"] = prompt_phone(site_url)
            continue

        result["ok"] = False
        result["phone_taken"] = http_is_phone_taken(resp_json)
        result["messages"] = [http_message_of(resp_json) or f"Register rejected: {resp_json}"]
        if attempts:
            result["messages"].append(f"Gave up after {attempts} retr{'y' if attempts == 1 else 'ies'}.")
        return result

    print(f"\nOTP requested — an SMS code was sent to {acct.get('phone', 'your phone')}.")
    print(f"  server says: {http_message_of(resp_json)}")
    otp = prompt_otp(prof.http_otp_digits)

    try:
        verify_json = http_verify_signup_otp(session, csrf, acct, url, otp, resp_json)
    except requests.RequestException as e:
        result["ok"] = False
        result["messages"] = [f"OTP verify request failed: {str(e)[:200]}"]
        return result

    if http_is_error(verify_json, url):
        result["ok"] = False
        result["messages"] = [f"OTP rejected: {http_message_of(verify_json) or verify_json}"]
        return result

    result["ok"] = True
    result["messages"] = [http_message_of(verify_json) or "OTP verified — account appears registered."]

    if free_number:
        ok, new_phone, msg = http_free_phone_number(session, csrf, url)
        result["freed_phone"] = new_phone if ok else None
        result["messages"].append(f"Free-number: {msg}" if ok else f"Free-number FAILED: {msg}")

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
    """Screenshot-saving is disabled globally -- see save_screenshot(). Kept
    as a no-op returning "" (not a path suffix) so callers embedding this in
    an error message don't reference a file that was never written."""
    return ""


# In-page check that the browser session is ACTUALLY authenticated, not just
# that the header "Account" link is present. Critical fix for a real bug: the
# old logged-in signal (#acctSec) is in the page header even when logged OUT,
# so login() returned "ok" for a rejected/throttled login. Downstream that
# meant change_account_password() fired on a logged-out session and got the
# site's session-less reply ("please add phone number ...") -- which looked
# like a phone problem but was really a failed login. Worse, it could mark a
# password change "successful" that never happened. This fetches the same
# getBalance endpoint the site's own JS calls; a 200 with a balance body only
# comes back for a genuinely authenticated session (a guest gets 401
# "Unauthenticated").
# Parses the getBalance JSON IN-PAGE and returns a clean verdict, so the full
# (long) response body never has to be shipped back and re-parsed in Python --
# an earlier version truncated the body to 300 chars, which corrupted the JSON
# and made every check fail. "authed" is only true for the authenticated
# shape {"status":200,"balance":{...}}; a guest gets 401 "Unauthenticated".
_AUTH_CHECK_FETCH_JS = """async (path) => {
    const meta = document.querySelector('meta[name=csrf-token]');
    const token = meta ? meta.content : '';
    try {
        const resp = await fetch(path, {
            method: "POST",
            credentials: "same-origin",
            headers: {
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "X-Requested-With": "XMLHttpRequest",
                "X-CSRF-TOKEN": token,
            },
            body: "_token=" + encodeURIComponent(token),
        });
        let authed = false;
        try {
            const body = await resp.json();
            authed = body && body.status === 200 && body.balance != null;
        } catch (e) { /* non-JSON (e.g. a block page) -> not authed */ }
        return {status: resp.status, authed: authed};
    } catch (e) {
        return {status: -1, authed: false};
    }
}"""


def _browser_login_authenticated(page, site_url):
    """True only if the current browser session is really logged in, verified
    by an in-page getBalance call (see _AUTH_CHECK_FETCH_JS). Returns None if
    it can't tell yet (network hiccup / navigation), so the caller keeps
    polling rather than treating an inconclusive check as a failure."""
    prof = profile_for(site_url or page.url)
    parts = urlsplit(site_url or page.url)
    path = f"{parts.scheme}://{parts.netloc}{prof.http_balance_path}"
    try:
        result = page.evaluate(_AUTH_CHECK_FETCH_JS, path)
    except Exception:
        return None
    status = result.get("status")
    if status == 200:
        return bool(result.get("authed"))
    if status in (401, 419):  # Unauthenticated / CSRF-expired => not logged in
        return False
    return None  # -1 (fetch threw) or anything else => unknown, keep polling


_WAF_LOGIN_MSG = ("AWS WAF refused the login request itself (HTTP {status}, "
                  "action=captcha) -- the site never saw these credentials")


def login_url_for(site_url=None):
    """Where this site's login form is. Usually the homepage; winclash needs
    /join-now, whose header login bar renders every time (the homepage's does
    not -- see SiteProfile.login_path). Callers that pre-navigate must use
    this, or login() will be looking for the form on the wrong page."""
    base = site_url or SITE_URL
    prof = profile_for(base)
    return urljoin(base, prof.login_path) if prof.login_path else base


def login(page, username, password, site_url=None, already_loaded=False):
    """Log into an EXISTING account (not signup). Returns (outcome, messages)
    where outcome is "ok", "error", or "timeout".

    `already_loaded=True` means the caller has ALREADY navigated this page to
    the site and dealt with whatever stands in front of it. That matters on a
    WAF-walled site (winclash): navigating again re-arms the interstitial, and
    a hard `captcha` action does not clear itself, so a caller that spent a
    CapSolver token getting through would have it thrown away here and the
    login would fail on a "Human Verification" page. Callers that have not
    navigated leave this False and get the original behaviour."""
    prof = profile_for(site_url or SITE_URL)
    if not (prof.supports_login or prof.supports_casino):
        return "error", [f"Login is not supported for {prof.key} "
                         "(its login selectors are not inspected)."]
    if not already_loaded:
        try:
            page.goto(login_url_for(site_url), wait_until="domcontentloaded",
                      timeout=60000)
        except PWError as e:
            return "error", [f"Couldn't load the site (check the proxy?): {str(e)[:150]}"]
        page.wait_for_timeout(4000)
        if prof.waf_on_navigation:
            # Ride out the soft challenge, which clears itself for free once
            # the page's own challenge.js has run. A hard captcha needs a
            # token and a fresh context, which only a caller owning the
            # context can do -- it surfaces below as "could not find the
            # LOGIN button", with the interstitial's own text attached.
            wait_out_waf_wall(page, 20)
    else:
        page.wait_for_timeout(1000)

    # Retry finding the LOGIN button over a longer window, dismissing popups
    # each pass. On a slow/remote (prod) machine the homepage can take longer to
    # render and a promo overlay can cover the button; a single 5s attempt then
    # fails. If it never appears, capture what the page actually is -- a WAF/403
    # block page (served to datacenter IPs) has no login button at all.
    clicked = False
    # Some sites render the login fields inline in the header rather than
    # behind a button, so there is nothing to open. On winclash that button is
    # the very same element as the form's SUBMIT (button.clsLoginClick is also
    # button.btnLogin), so clicking it here would submit an empty form and the
    # real attempt would land on a site already complaining. If the username
    # field is on screen, the form is open -- go straight to filling it.
    try:
        clicked = page.locator(prof.sel["login_username"]).first.is_visible()
    except Exception:
        clicked = False
    deadline = time.time() + 20
    while not clicked and time.time() < deadline:
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

    dismiss_popups(page)
    if prof.login_click_mode == "js":
        # Overlay-covered UI (see SiteProfile.login_click_mode): fill() sets
        # the value without a pointer event, so it is unaffected by the
        # backdrop that makes click() -- even a forced one -- unusable here.
        page.fill(prof.sel["login_username"], username)
        page.fill(prof.sel["login_password"], password)
    else:
        user_field = page.locator(prof.sel["login_username"])
        user_field.click()
        user_field.press_sequentially(username, delay=30)
        pass_field = page.locator(prof.sel["login_password"])
        pass_field.click()
        pass_field.press_sequentially(password, delay=30)
    def _submit_login():
        """Fire the login. Returns False only if the button wasn't there."""
        if prof.login_click_mode == "js":
            # See SiteProfile.login_click_mode: on winclash a full-page
            # overlay eats both a normal AND a forced click, so the button is
            # clicked from inside the page, which bypasses hit-testing.
            return bool(page.evaluate(
                "(sel) => { const e = document.querySelector(sel);"
                " if (!e) return false; e.click(); return true; }",
                prof.sel["login_submit"]))
        page.locator(prof.sel["login_submit"]).click(timeout=5000)
        return True

    # Watch for the login/balance XHR being refused by AWS WAF rather than by
    # the application. That comes back as 405 (or 403) carrying an
    # x-amzn-waf-action header, and the site renders its generic "Something
    # went wrong" toast for it -- indistinguishable, from the page alone,
    # from a real rejection. Confirmed live 2026-08-29: winclash's WAF starts
    # captcha-gating POST /api2/v2/login after a stretch of repeated logins
    # from one IP, the same behavioural/rate pattern spin24star's register
    # POST shows. The caller can fix this (clear the wall, retry from a fresh
    # context); a wrong password it cannot, so the two must not be conflated.
    waf_blocked = []

    def _watch_waf(resp):
        try:
            if resp.headers.get("x-amzn-waf-action") and resp.status in (403, 405):
                waf_blocked.append(resp.status)
        except Exception:
            pass

    try:
        page.context.on("response", _watch_waf)
    except Exception:
        pass

    _submit_login()

    # Wait for a DEFINITIVE outcome. Order matters: check for a site error
    # message first (so a rejected login reports the real reason -- e.g.
    # "Invalid Username or Password" -- instead of silently timing out), then
    # confirm the session is genuinely authenticated. The header "Account"
    # link (logged_in_indicator) is NOT a reliable success signal on its own:
    # it's present when logged out too, so relying on it made login() return
    # "ok" for failed/throttled logins (see _browser_login_authenticated).
    can_verify_auth = prof.supports_http_login or prof.verify_auth_in_page
    # The verified-auth path needs to ride out the post-login redirect (which
    # briefly destroys the page context) plus residential-proxy latency, so
    # give it a longer window than the plain indicator check.
    deadline = time.time() + (30 if can_verify_auth else 12)
    retries, next_retry = 0, time.time() + 6
    while time.time() < deadline:
        msgs = [m for m in read_result(page)
                if not any(b in m.lower() for b in prof.benign_texts)]
        if msgs:
            if waf_blocked:
                return "waf", [_WAF_LOGIN_MSG.format(status=waf_blocked[0])]
            return "error", msgs
        if can_verify_auth:
            authed = _browser_login_authenticated(page, site_url or SITE_URL)
            if authed is True:
                _dismiss_stuck_login_modal(page)
                return "ok", []
            # authed is False (definitely not logged in) or None (unknown yet)
            # -> keep polling; a real error will surface via read_result(), or
            # we fall through to a timeout below.
        else:
            # Sites without a verifiable auth endpoint fall back to the header
            # indicator (best available signal for them).
            try:
                if page.locator(prof.sel["logged_in_indicator"]).first.is_visible():
                    _dismiss_stuck_login_modal(page)
                    return "ok", []
            except Exception:
                pass
        # An in-page click can land before the site has bound its own handler
        # to the button, in which case nothing at all happens -- no request,
        # no error, no message (observed on the production server, where the
        # page hydrates more slowly than on a laptop). Re-firing costs
        # nothing: a duplicate login POST is idempotent, and this only runs
        # while the session still isn't authenticated.
        if (prof.login_click_mode == "js" and time.time() >= next_retry
                and retries < 3):
            retries += 1
            next_retry = time.time() + 6
            try:
                _submit_login()
            except Exception:
                pass
        page.wait_for_timeout(400)
    if waf_blocked:
        return "waf", [_WAF_LOGIN_MSG.format(status=waf_blocked[0])]
    if can_verify_auth:
        return "timeout", ["Login did not complete (session never became "
                           "authenticated -- credentials rejected or the login "
                           "was throttled)."]
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
    prof = profile_for(page.url)
    if prof.casino_lobby_mode == "direct_url":
        return _open_casino_lobby_direct(page, prof, timeout_ms)

    dismiss_popups(page)
    casino_nav = prof.sel["casino_nav"]
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


def _open_casino_lobby_direct(page, prof, timeout_ms=15000):
    """Reach the Live Casino lobby by navigating straight to it.

    Used where casino_lobby_mode == "direct_url" (starexch). Everything the
    cricmatch path does is wrong here and was confirmed so live 2026-08-19:
    no "Live Casino" nav element responds to a click (they time out or
    no-op), while a plain goto works AND keeps the session -- the opposite of
    cricmatch, where a hard load drops the logged-in view. Readiness is
    therefore checked on the tiles themselves rather than on a category tab,
    because this lobby has no category tabs: the provider is in the URL.

    Returns True once at least one openable tile is present."""
    parts = urlsplit(page.url)
    lobby_url = f"{parts.scheme}://{parts.netloc}{prof.casino_lobby_path}"
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        try:
            page.goto(lobby_url, wait_until="domcontentloaded", timeout=30000)
        except PWError:
            # ERR_ABORTED shows up when a previous navigation is still
            # settling right after login -- worth one more try, not a failure.
            page.wait_for_timeout(2000)
            continue
        page.wait_for_timeout(3000)
        dismiss_popups(page)
        try:
            if page.locator("[onclick*='goToCasinoLive']").count() > 0:
                return True
        except Exception:
            pass
        page.wait_for_timeout(1000)
    return False


def _click_tile_go_to_casino_live(page, tile_text):
    """Open a game tile by invoking the site's own handler.

    starexch binds the click to div[onclick="goToCasinoLive(this)"], which
    sits on the tile's IMAGE container. The visible <p class="game__name">
    label is inert -- clicking it silently does nothing at all, which reads
    as "the tile didn't open" rather than as an error (cost a probe run to
    find). Matching is on the tile's exact name, so "Baccarat A" can't pick
    up "All In Baccarat".

    Returns the tile's data-id on success, or "" if no such tile is visible.
    """
    return page.evaluate("""(want) => {
      const els = [...document.querySelectorAll('[onclick*="goToCasinoLive"]')];
      const t = els.find(e => {
        const par = e.closest('.partclrGamesParDv,.partclrGamesParDvMob');
        const nm = par && par.querySelector('.game__name');
        return nm && nm.innerText.trim() === want
               && e.getBoundingClientRect().width > 5;
      });
      if (!t) return '';
      t.click();
      return t.getAttribute('data-id') || '?';
    }""", tile_text)


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
    prof = profile_for(page.url)
    _dismiss_casino_promo_modal(page)
    try:
        if prof.casino_tile_mode == "go_to_casino_live":
            # No category tabs in this lobby -- `category` is unused here, the
            # provider was already chosen in the lobby URL.
            if not _click_tile_go_to_casino_live(page, tile_text):
                return None
            page.wait_for_timeout(1000)
            _dismiss_choose_chips_modal(page)
        else:
            page.locator(f"a:has-text('{category}')").first.click(timeout=5000, force=True)
            page.wait_for_timeout(2500)
            _dismiss_casino_promo_modal(page)
            page.locator(f"text={tile_text}").first.click(timeout=5000, force=True)
            page.wait_for_timeout(1000)
            _dismiss_choose_chips_modal(page)
    except Exception:
        return None

    deadline = time.time() + 30
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
    // Two vocabularies, probed live: baccarat/dragon-tiger/stock-market spots
    // are [data-role="bet-spot-..."], while roulette's board is an SVG whose
    // spots carry [data-bet-spot-id="red"/"black"/...] instead (captured on
    // starexch's Auto-Roulette 2026-08-25). Role names never collide across
    // the two, so try both.
    const el = document.querySelector(`[data-role="${role}"]`)
            || document.querySelector(`[data-bet-spot-id="${role}"]`);
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
                // data-bet-spot-id is roulette's spot vocabulary -- see
                // _TAG_BET_SPOT_JS.
                const spot = document.querySelector(`[data-role="${readyRole}"]`)
                    || document.querySelector(`[data-bet-spot-id="${readyRole}"]`);
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


def test_baccarat(browser, username, password, amount, site_url=None,
                  category="Baccarat", tile_text="Baccarat A", proxy_conf=None,
                  proxy=None):
    """Smoke-test the casino game integration: log in, open a Baccarat table,
    and place `amount` on both Player and Banker. Returns a result dict in
    the same shape convention as signup_once(): {"ok", "messages", "shot"}.
    Does not wait for the round to resolve -- confirms placement, not outcome.

    Takes a BROWSER, not a page, because getting to the table is site-specific
    and _open_table_for already owns every one of those differences -- it is
    the exact route /run's hedge setup uses. This used to open the lobby and
    click a tile inline, which is cricmatch's route and only cricmatch's: on
    winclash there is no usable tile at all (tables launch by URL, see
    SiteProfile.casino_launch_mode) and every page load can be met by an AWS
    WAF interstitial that has to be cleared before a login button even exists.
    Sharing the route means /testbaccarat can no longer fail on a site where
    /run works, or vice versa.

    Does not write to accounts.db -- this tests an EXISTING account, not a
    newly created one, a different data lifecycle than the rest of this file."""
    result = {"ok": False, "messages": [], "shot": None}

    try:
        context, page, game_page, frame = _open_table_for(
            browser, username, password, site_url, category, tile_text,
            proxy_conf=proxy_conf, proxy=proxy, game=BACCARAT)
    except RuntimeError as e:
        # _open_table_for closes its own context on every failure path and
        # names the phase that failed (login / WAF / lobby / tile / frame).
        result["messages"] = [str(e)]
        return result

    try:
        bet_result = place_baccarat_bet(game_page, frame, amount)
        result["ok"] = bet_result["ok"]
        result["messages"] = bet_result["messages"]

        stamp = time.strftime("%Y%m%d-%H%M%S")
        result["shot"] = save_screenshot(game_page, SHOTS_DIR / f"{username}-{stamp}-baccarat-bet.png")
        return result
    finally:
        try:
            context.close()
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


def select_chip_fast(frame, value, timeout_secs=5):
    """Select one chip denomination, with a SHORT deadline.

    select_chip() above waits up to 75s, which is right when it runs once
    before the round loop but far too long to sit inside a ~10s betting
    window. Multi-chip stakes have to switch denomination mid-window (see
    _place_stake), so they need this variant -- a failure has to give up fast
    enough for the caller to notice a short stake and retry the round, rather
    than eating the whole window."""
    deadline = time.time() + timeout_secs
    while time.time() < deadline:
        try:
            if read_chips(frame).get("selected") == value:
                return True
            loc = frame.locator(f'[data-role="chip"][data-value="{value}"]')
            if loc.count():
                loc.first.click(timeout=1500, force=True)
        except Exception:
            pass
        time.sleep(0.25)
    return read_chips(frame).get("selected") == value


def _place_stake(frame, role, groups):
    """Click one side's whole stake onto its bet spot.

    `groups` is [(chip, count), ...] largest chip first (chip_plan.group_plan),
    so a ₹1,300 stake on the ₹10/50/100/200/500/2500 rail is two ₹500 clicks,
    one ₹200 and one ₹100 -- that's how an amount that ISN'T a single chip
    value gets bet at all. An empty `groups` means the game has no selectable
    rail (baccarat here), so it falls back to the original single click on
    whatever chip the table has pre-selected.

    Returns True only if every planned click was issued. False means the stake
    landed SHORT, not that nothing landed -- the caller must still read TOTAL
    BET and compare both sides, since a short stake on one side is real
    exposure."""
    if not groups:
        return _click_bet_spot(frame, role)
    for chip, count in groups:
        if not select_chip_fast(frame, chip):
            return False
        for _ in range(count):
            if not _click_bet_spot(frame, role):
                return False
    return True


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


def _click_cashout(frame, game=None, timeout=5000, debug=None):
    """Click CASH OUT. Same tag-then-force-click approach as _click_bet_spot
    (a decorative overlay can sit above it). Returns True if the element was
    found and clicked -- NOT proof the cash-out registered; the caller must
    verify the portfolio dropped back to 0.

    `debug`, if given a dict, records why a False came back (tag-not-found
    vs. an exception and its message) -- added because a real paired run
    (Pair #4, run #9) reported "neither side cashed out" with zero detail on
    which of those it was, making the failure undiagnosable after the fact."""
    game = game or STOCKMARKET
    try:
        if not frame.evaluate(_TAG_BET_SPOT_JS, game.cashout_role):
            if debug is not None:
                debug["reason"] = "tag_not_found"
            return False
        frame.locator(f'[data-pw-spot="{game.cashout_role}"]').click(
            timeout=timeout, force=True)
        return True
    except Exception as e:
        if debug is not None:
            debug["reason"] = "exception"
            debug["error"] = str(e)[:200]
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
    """Close the context and raise. Screenshot-saving is disabled globally
    (see save_screenshot()), so unlike before this no longer captures
    shots/hedge-setup-*.png evidence of an intermittent setup failure."""
    context.close()
    raise RuntimeError(reason)


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
                # Same dual lookup as _TAG_BET_SPOT_JS: roulette's spots are
                # [data-bet-spot-id=...], not [data-role=...]. Without it this
                # loop can never see an opened roulette table ready.
                if fr.evaluate(
                        "(r) => !!(document.querySelector(`[data-role=\"${r}\"]`)"
                        " || document.querySelector(`[data-bet-spot-id=\"${r}\"]`))",
                        game.table_ready_role):
                    return fr
            except Exception:
                continue
        game_page.wait_for_timeout(500)
    raise RuntimeError(f"{game.lobby_tile} opened but its frame never appeared")


def _open_game_direct(page, prof, tile_text, site_url=None):
    """Open a live table by navigating straight to the site's own game-launch
    redirect, skipping the lobby and its tile entirely. Returns the new page
    (the game tab) or raises RuntimeError.

    Used only where SiteProfile.casino_launch_mode == "direct_game_url"
    (winclash). Mapped live 2026-08-29 by reading winclash's own .casinoLink
    click handler: it builds
        /casinoRedirect?q=<game id>&provider=<provider>&type=casino
    and hands it to window.open(url, "_blank"). Doing the navigation directly
    is both steadier than the tile (which is hover-only and paginated behind a
    search box) and strictly more capable: the handler bails out to an "add
    money" popup when the account's balance is zero, whereas the redirect
    still launches the table.

    A NEW TAB is opened rather than navigating the current one, so the caller
    keeps its logged-in main page -- every other route here yields a separate
    game tab too, and _open_table_for's callers close the two independently."""
    game_id = prof.casino_game_ids.get(tile_text)
    if not game_id:
        known = ", ".join(sorted(prof.casino_game_ids)) or "none"
        raise RuntimeError(
            f"{prof.key} has no game id recorded for {tile_text!r} "
            f"(known tables: {known}) -- read its id from the site's own "
            f"/casinoGamesList and add it to the profile")
    parts = urlsplit(site_url or SITE_URL)
    path = prof.casino_launch_path.format(id=game_id, provider=prof.casino_provider)
    url = f"{parts.scheme}://{parts.netloc}{path}"
    game_page = page.context.new_page()
    try:
        game_page.goto(url, wait_until="domcontentloaded", timeout=60000)
    except PWError as e:
        try:
            game_page.close()
        except Exception:
            pass
        raise RuntimeError(f"could not open the {tile_text!r} table: {str(e)[:150]}")
    # The redirect lands on the provider's own host; give it a moment to
    # actually get there before the caller starts hunting for the game frame.
    for _ in range(20):
        game_page.wait_for_timeout(1000)
        if "evo-games.com" in (game_page.url or ""):
            return game_page
    # Still on the operator's own domain -> the launch was refused. Say what
    # the page shows, since that is where the real reason lives (a zero
    # balance, a blocked account, a game that is off).
    try:
        why = (game_page.inner_text("body") or "").strip()[:200].replace("\n", " ")
    except Exception:
        why = ""
    raise RuntimeError(
        f"the {tile_text!r} launch did not reach the game provider "
        f"(still at {game_page.url[:120]}){': ' + why if why else ''}")


def _open_table_for(browser, username, password, site_url, category, tile_text,
                    proxy_conf=None, progress=None, label="", game=None,
                    proxy=None):
    """Log a fresh context into `username` and open the given live table.
    Returns (context, main_page, game_page, frame) or raises RuntimeError with
    a human-readable reason. Caller owns closing the context. `proxy_conf` is a
    Playwright proxy dict (already bridged for SOCKS5-auth) or None for direct.
    `proxy` is the RAW proxy string that conf came from, and both are needed on
    a WAF-walled site: Chromium gets the (possibly bridged, 127.0.0.1) conf,
    while CapSolver must be told the real upstream so the token is minted from
    the egress IP it will be used from -- AWS WAF tokens can be IP-bound. It is
    also the token cache key, so seats on different proxies never reuse each
    other's token. Same deliberate raw+conf pairing as the signup paths.
    `progress(str)` (optional), if given, is called once per phase (login,
    casino lobby, game join, live table) -- this whole function is naturally
    slow (a real login + a real live-video game loading, done sequentially for
    two accounts, easily 1-2+ min each over a proxy), so without these the
    chat goes silent for minutes with no sign anything is happening."""
    progress = progress or (lambda _msg: None)
    game = game or BACCARAT
    tag = f" ({label})" if label else ""
    progress(f"🔑 Logging in{tag}…")
    prof = profile_for(site_url or SITE_URL)
    # new_site_context, not a bare new_context: winclash's WAF flat-403s
    # headless Chromium's default user agent, so without the profile's UA the
    # login below never even gets served a page. No-op for the other sites.
    context = new_site_context(browser, site_url or SITE_URL, proxy_conf=proxy_conf)
    page = context.new_page()
    if prof.waf_on_navigation:
        # Get past the navigation interstitial FIRST. login() would otherwise
        # spend its whole timeout hunting a LOGIN button on a "Human
        # Verification" page. Note ensure_waf_cleared can hand back a page in
        # a brand-new context (a solved token only works in a fresh one), so
        # `context` is rebound to whatever the page ends up in -- the caller
        # closes that, and the old one is already closed for us.
        try:
            page.goto(login_url_for(site_url), wait_until="domcontentloaded",
                      timeout=60000)
        except PWError as e:
            context.close()
            raise RuntimeError(f"could not load the site for {username}: {str(e)[:150]}")
        page, waf_ok, waf_msg = ensure_waf_cleared(
            page, site_url or SITE_URL, proxy=proxy, proxy_conf=proxy_conf)
        context = page.context
        if not waf_ok:
            context.close()
            raise RuntimeError(f"blocked before login for {username}: {waf_msg}")
    outcome, msgs = login(page, username, password, site_url=site_url,
                          already_loaded=prof.waf_on_navigation)
    if outcome == "waf":
        # AWS WAF refused the login CALL, not the credentials. Only the owner
        # of the context can fix that: mint a fresh token and start over in a
        # clean context (a token injected into the context that was
        # challenged is still refused -- see ensure_waf_cleared). Worth
        # exactly one retry; if the wall is still up after a solve, the IP is
        # flagged and another attempt just spends more CapSolver credit.
        page, waf_ok, waf_msg = ensure_waf_cleared(
            page, site_url or SITE_URL, proxy=proxy, proxy_conf=proxy_conf,
            force=True)
        context = page.context
        if waf_ok:
            try:
                page.goto(login_url_for(site_url), wait_until="domcontentloaded",
                          timeout=60000)
            except PWError:
                pass
            outcome, msgs = login(page, username, password, site_url=site_url,
                                  already_loaded=True)
        else:
            msgs = [f"{msgs[0] if msgs else 'AWS WAF blocked the login'}; "
                    f"clearing it failed too: {waf_msg}"]
    if outcome != "ok":
        context.close()
        raise RuntimeError(f"login failed for {username}: {'; '.join(msgs) or outcome}")
    game_page = None
    if prof.casino_launch_mode == "direct_game_url":
        # winclash: no lobby, no tile -- the site's own launch redirect opens
        # the table directly. See SiteProfile.casino_launch_mode for why this
        # is preferred there (a hover-only tile, and a click handler that
        # refuses to launch on a zero balance).
        # The direct route goes straight to the FINAL table. Games that reach
        # their table through the provider's in-game lobby (Stock Market on
        # cricmatch) name the entry game in tile_text and the real one in
        # lobby_tile; here that second hop is unnecessary, so the real one is
        # what gets launched.
        want = game.lobby_tile if (game.via_provider_lobby and game.lobby_tile) else tile_text
        progress(f"🃏 Opening {want}{tag}…")
        try:
            game_page = _open_game_direct(page, prof, want, site_url)
        except RuntimeError as e:
            context.close()
            raise RuntimeError(f"{e} (for {username})")

    if game_page is None:
        progress(f"🎰 Opening the Live Casino{tag}…")
        # The Live Casino nav is intermittently flaky in stretches (site-side; see
        # open_casino_lobby's docstring). Recovery ladder: (1) the SPA click loop,
        # (2) a direct goto to /live-casino -- documented risk is landing on a
        # logged-out view, so (3) detect that and re-login in place, then one more
        # click loop. On top of this, _open_table_with_retry() retries the whole
        # thing from a fresh context.
        if not open_casino_lobby(page, timeout_ms=20000):
            parts = urlsplit(site_url or SITE_URL)
            # Follow the site's own lobby path, not a bare /live-casino: on
            # starexch the provider in the query string is what separates the
            # Evolution tables this engine can drive from a third-party baccarat
            # it cannot. Readiness is likewise checked the way that site's lobby
            # actually renders -- an <a>Baccarat</a> exists only on cricmatch's.
            try:
                page.goto(f"{parts.scheme}://{parts.netloc}{prof.casino_lobby_path}"
                          if prof.casino_lobby_mode == "direct_url"
                          else f"{parts.scheme}://{parts.netloc}/live-casino",
                          wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(3000)
                dismiss_popups(page)
            except Exception:
                pass
            lobby_ok = False
            try:
                if prof.casino_tile_mode == "go_to_casino_live":
                    lobby_ok = page.locator("[onclick*='goToCasinoLive']").count() > 0
                else:
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
    # Skipped on the direct route, which already landed on the real table.
    if game.via_provider_lobby and prof.casino_launch_mode != "direct_game_url":
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
                           should_stop=None, game=None, proxy=None):
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
                                   game=game, proxy=proxy)
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


# Chromium throttles timers, rAF and rendering in any page it considers
# backgrounded or occluded. A live Evolution table signals its betting window
# through [data-role="circle-timer"], an ANIMATED element -- so a throttled
# page keeps answering DOM queries (no errors, no dead frame) while never
# drawing the timer at all. That is exactly the "no betting window opened in
# time, timer=None" fault, and it is why the give-up ladder's `blind` state
# could not tell it apart from a table that simply wasn't dealing.
#
# Measured live on starexch 2026-08-19, 5 seats on one table for 100s, one
# browser per seat, sampling once a second:
#     seat 1 (opened first)   0/98 samples saw the timer
#     seat 2                  0/98
#     seat 3                 22/98
#     seat 4                  7/98
#     seat 5 (opened last)   33/98      <- matches a lone seat's 30/99
# Zero errors on every seat. A single seat on the same table saw 30/99, so
# the table was dealing normally throughout; the gradient tracks how long a
# page had been sitting un-focused, not anything about the account or the
# table.
#
# These three flags are the standard Chromium answer to that. Keep them on
# every launch that will hold a live table open.
_ANTI_THROTTLE_ARGS = [
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
]


def _launch_pw_browser():
    """Start a standalone Playwright connection + headless Chromium. Must be
    called from (and every object it returns used from) the thread that will
    own it -- run this via executor.submit, never inline. Used to give the
    Player side of a paired hedge its own browser/thread so its login +
    table-join can run concurrently with the Banker side instead of
    serialized on one thread (see run_paired_hedge)."""
    pw = sync_playwright().start()
    try:
        browser = pw.chromium.launch(headless=True, args=_ANTI_THROTTLE_ARGS)
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
                proxy_conf=proxy_conf, should_stop=should_stop, game=game,
                proxy=proxy)
            try:
                banker_open = _open_table_with_retry(
                    browser, banker_creds, site_url, category, tile_text,
                    game.side_a_label, setup_progress, proxy_conf=proxy_conf,
                    should_stop=should_stop, game=game, proxy=proxy)
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

        # Work out HOW to bet `amount` BEFORE any betting. Without this the
        # table bets its pre-selected chip (the ₹10 minimum) whatever was
        # requested, which is what tripped amount_mismatch on the first run.
        #
        # `amount` used to have to BE one of the rail's chip values (₹10, 50,
        # 100, 200, 500 or 2500 on Stock Market Live) -- anything else was
        # refused outright. It is now built from several chips where needed
        # (₹150 = 100 + 50, ₹1,300 = 500 + 500 + 200 + 100), so any amount the
        # rail can actually reach is bettable. The plan is computed once here
        # and replayed identically on both sides every round, so the two
        # stakes stay equal and the hedge holds.
        stake_groups = []       # [(chip, count)]; empty = no rail, single click
        _clicks_per_side = 1
        if game.selectable_chips:
            p_fut = player_exec.submit(read_chips, fr_p)
            rail_b = read_chips(fr_b)
            rail_p = p_fut.result()
            avail = sorted(set(rail_b.get("chips") or []) & set(rail_p.get("chips") or []))
            if avail:
                stake, plan = plan_stake(amount, avail, table_min=min(avail),
                                         table_max=amount,
                                         max_clicks=HEDGE_MAX_BET_CLICKS)
                if stake != amount:
                    # Refuse rather than quietly betting a different size.
                    # Two ways to get here: below the table minimum, or not
                    # reachable within the click budget (a window is only
                    # ~10s wide -- see HEDGE_MAX_BET_CLICKS).
                    summary["stop_reason"] = "amount_mismatch"
                    rail_txt = ", ".join(f"₹{c}" for c in avail)
                    if stake == 0:
                        detail = (f"it's below this table's minimum (₹{min(avail)})"
                                  if amount < min(avail) else
                                  "no combination of this table's chips reaches it")
                    else:
                        detail = (f"the closest this table's chips reach within "
                                  f"{HEDGE_MAX_BET_CLICKS} clicks is ₹{stake:,}")
                    summary["messages"].append(
                        f"₹{amount:,} can't be staked on this table: {detail}. "
                        f"Chips: {rail_txt}. Re-run with a reachable amount. "
                        "(Aborted before any bet.)")
                    return summary
                stake_groups = group_plan(plan)
                _clicks_per_side = sum(n for _, n in stake_groups)
                if len(stake_groups) > 1 or stake_groups[0][1] > 1:
                    summary["messages"].append(
                        f"₹{amount:,}/side is built from "
                        + " + ".join(f"₹{c:,}×{n}" for c, n in stake_groups)
                        + f" ({sum(n for _, n in stake_groups)} clicks per side).")
            # Pre-select the plan's largest chip so a single-chip stake needs
            # no denomination switch inside the betting window at all (the
            # common case); multi-chip stakes switch mid-window via
            # select_chip_fast in _place_stake.
            first_chip = stake_groups[0][0] if stake_groups else amount
            p_fut = player_exec.submit(select_chip, fr_p, first_chip)
            ok_b = select_chip(fr_b, first_chip)
            ok_p = p_fut.result()
            if not (ok_b and ok_p):
                side = game.side_a_label if not ok_b else game.side_b_label
                bad_fr, bad_exec = ((fr_b, None) if not ok_b else (fr_p, player_exec))
                rail = (bad_exec.submit(describe_chip_rail, bad_fr).result()
                        if bad_exec else describe_chip_rail(bad_fr))
                summary["stop_reason"] = "chip_select_failed"
                summary["messages"].append(
                    f"Could not select the ₹{first_chip} chip on the {side} side "
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
            short = False       # both sides equal but under `amount` (hedged)
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

                # Fire the Player side on its own thread first (non-blocking),
                # then the Banker side inline -- both stakes land within the same
                # window instead of one waiting on the other. `stake_groups` is
                # usually a single click; a multi-chip amount replays the same
                # plan on both sides, so they stay equal.
                p_fut = player_exec.submit(_place_stake, fr_p, game.side_b_role,
                                           stake_groups)
                _place_stake(fr_b, game.side_a_role, stake_groups)
                p_fut.result()
                # Poll rather than sleeping a flat 1.5s: a multi-chip stake is
                # several clicks per side, so TOTAL BET reaches its final value
                # later than a single click's does, and reading too early would
                # look like a short/unequal stake that isn't one.
                tb_b = tb_p = None
                read_deadline = time.time() + 5
                while True:
                    time.sleep(1.0)
                    p_fut = player_exec.submit(_read_total_bet, fr_p)
                    tb_b = _read_total_bet(fr_b)
                    tb_p = p_fut.result()
                    if (tb_b == amount and tb_p == amount) or time.time() >= read_deadline:
                        break

                if tb_b == amount and tb_p == amount:
                    placed = True
                    break
                got_b, got_p = tb_b or 0, tb_p or 0
                if got_b == got_p and got_b > 0:
                    # Both sides staked the SAME wrong size. Still a true hedge
                    # (no exposure), just not the size asked for.
                    #
                    # Two different causes, handled differently. With a
                    # multi-chip stake it means the betting window closed part
                    # way through both sides' click sequence -- a timing blip,
                    # so wait out the hand and retry the round slot, exactly
                    # like any other missed window. With a single-click stake
                    # there is nothing to be part-way through: the table simply
                    # bet a chip that isn't `amount` (a game with no rail at
                    # all, or an unexpected mid-run reset), and repeating would
                    # just place the same wrong size again -- so stop and name
                    # the real size.
                    if _clicks_per_side > 1:
                        short = True
                        summary["messages"].append(
                            f"Attempt {attempt}: the window closed part way through "
                            f"the stake -- ₹{got_b} landed on each side instead of "
                            f"₹{amount:,}. Both sides are equal, so the hand is still "
                            "hedged and nothing is exposed. Waiting for it to settle, "
                            "then retrying.")
                        _screenshot_pair(gp_b, gp_p, summary,
                                         f"short-attempt{attempt}", player_exec)
                        break
                    summary["stop_reason"] = "amount_mismatch"
                    summary["messages"].append(
                        f"The table placed ₹{tb_b} per side (its selected chip), not the "
                        f"requested ₹{amount}. That one round is hedged; stopping. Re-run "
                        f"with amount={tb_b}.")
                    _screenshot_pair(gp_b, gp_p, summary, "mismatch", player_exec)
                    return summary
                if got_b != got_p:
                    # The two sides staked DIFFERENT amounts -> the difference
                    # is real, one-sided exposure for this one hand. Covers
                    # both "only one side landed at all" and (new, with
                    # multi-chip stakes) "one side got fewer chips down than
                    # the other before the window closed".
                    #
                    # Baccarat hands resolve on their own (no button to press,
                    # unlike Stock Market's live position) -- so unlike a
                    # cash-out failure, there is nothing further waiting makes
                    # worse. Don't count it as a hedged round; wait for this
                    # hand to settle below, then retry the round slot.
                    unhedged = True
                    if got_b > got_p:
                        exposed, side = banker_creds["username"], game.side_a_label
                    else:
                        exposed, side = player_creds["username"], game.side_b_label
                    gap = abs(got_b - got_p)
                    landed = (f"only the {side} bet landed" if min(got_b, got_p) == 0
                              else f"the two sides landed unequally (₹{got_b} vs ₹{got_p})")
                    summary["messages"].append(
                        f"Attempt {attempt}: {landed} (account {exposed} exposed for "
                        f"₹{gap} this hand, not counted as hedged). Waiting for it to "
                        "settle, then retrying.")
                    summary.setdefault("unhedged_rounds", []).append(
                        {"attempt": attempt, "side": side, "account": exposed,
                         "amount": gap})
                    _screenshot_pair(gp_b, gp_p, summary,
                                     f"partial-attempt{attempt}", player_exec)
                    break
                # neither landed (window closed) -> retry the window.
                time.sleep(2)

            if summary["stop_reason"] == "stopped_by_user":
                break

            if unhedged or short:
                # Wait for the hand to resolve before trying again -- same
                # settle-wait the normal end-of-round path uses below -- so the
                # retry starts on a clean board, not mid-hand. `short` (both
                # sides equal but under `amount`) carries no exposure, but the
                # hand is still live, so it waits the same way.
                for _ in range(game.settle_secs):
                    p_fut = player_exec.submit(_read_total_bet, fr_p)
                    b_tb = _read_total_bet(fr_b)
                    if (b_tb or 0) == 0 and (p_fut.result() or 0) == 0:
                        break
                    time.sleep(1)
                consecutive_failures += 1
                if consecutive_failures >= MAX_CONSECUTIVE_ROUND_FAILURES:
                    if unhedged:
                        summary["stop_reason"] = "repeated_unhedged_exposure"
                        summary["messages"].append(
                            f"{consecutive_failures} unhedged exposures in a row -- "
                            "stopping rather than risking more.")
                    else:
                        # Nothing was ever exposed, but the stake keeps not
                        # fitting in the window -- that's the amount being too
                        # many chips for this table, not a blip.
                        summary["stop_reason"] = "short_stake"
                        summary["messages"].append(
                            f"{consecutive_failures} rounds in a row where the betting "
                            f"window closed part way through the ₹{amount:,} stake "
                            f"({_clicks_per_side} chip clicks per side). Every one of "
                            "those hands was still hedged -- nothing was exposed. Re-run "
                            "with an amount that needs fewer chips.")
                    break
                progress(f"⚠️ Attempt {attempt}: "
                         + ("stake landed short (still hedged)" if short
                            else "unhedged exposure")
                         + f", retrying ({summary['rounds_done']}/{rounds} hedged so far)…")
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
                #
                # A real paired live test (2026-07-21, ₹10/side, pair #1) added
                # click diagnostics and found the actual failure mode: the click
                # WAS found and fired on both sides every time (clicked=True),
                # but never registered -- portfolio stayed exactly at the staked
                # amount (₹10) the whole round. The click-diagnostic trace showed
                # readiness (window closed + portfolio>0) was reached the instant
                # betting closed, while portfolio was still == the stake -- i.e.
                # before the position had actually gone live server-side.
                # probe_live_cashout.py's ONE working cash-out (2026-07-20) only
                # ever attempted a click after explicitly confirming the
                # portfolio had diverged from the staked value (a real "MOVING"
                # check) -- that requirement was never carried over into this
                # round loop's readiness gate. Added here: track each side's
                # first-seen portfolio value once window-closed+portfolio>0,
                # and require it to move away from that value before treating
                # the position as actually cashable.
                co_deadline = time.time() + game.settle_secs
                ready = False
                co_trace = []          # phase/portfolio/opacity, for diagnosis
                co_live_dump = None    # button markup while a position rides
                first_port_b = first_port_p = None
                moved_b = moved_p = False
                while time.time() < co_deadline:
                    if should_stop():
                        break
                    p_fut = player_exec.submit(read_portfolio, fr_p, game)
                    port_b_now = read_portfolio(fr_b, game)
                    port_p_now = p_fut.result()
                    p_open_fut = player_exec.submit(_betting_open, fr_p, game)
                    b_closed = not _betting_open(fr_b, game)
                    p_closed = not p_open_fut.result()

                    b_pos = b_closed and port_b_now is not None and port_b_now > 0
                    p_pos = p_closed and port_p_now is not None and port_p_now > 0
                    if b_pos and first_port_b is None:
                        first_port_b = port_b_now
                    if p_pos and first_port_p is None:
                        first_port_p = port_p_now
                    if (b_pos and first_port_b is not None
                            and abs(port_b_now - first_port_b) > 0.01):
                        moved_b = True
                    if (p_pos and first_port_p is not None
                            and abs(port_p_now - first_port_p) > 0.01):
                        moved_p = True

                    # Record how the three signals move, so a failure says WHY
                    # rather than just "it didn't work" (the button's enabled
                    # state is invisible in a screenshot once the run has torn
                    # the context down).
                    snap = (_read_instruction(fr_b, game.instruction_role),
                            port_b_now,
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
                    if b_pos and p_pos and moved_b and moved_p:
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

                # Persistent retry, not just one or two clicks. A live test
                # (2026-07-21, pair #1) confirmed the click DOES land (found +
                # fired, no exception) yet does not register on the first try
                # or two even with a confirmed-live, moving position. The one
                # cash-out that WAS ever confirmed working (probe_live_cashout.py,
                # 2026-07-20, single account) needed clicks roughly every 1.5s
                # over up to ~75s before one actually took -- so keep clicking
                # whichever side(s) are still open at that same cadence, each
                # side stopping independently the moment ITS OWN portfolio
                # confirms closed, for up to CASHOUT_CLICK_WINDOW_SECS.
                CASHOUT_CLICK_WINDOW_SECS = 30
                CASHOUT_CLICK_INTERVAL = 1.5
                click_attempts = []
                still_b = still_p = True
                click_deadline = time.time() + CASHOUT_CLICK_WINDOW_SECS
                attempt_no = 0
                while (still_b or still_p) and time.time() < click_deadline:
                    attempt_no += 1
                    dbg_b, dbg_p = {}, {}
                    clk_b = clk_p = None
                    p_fut = (player_exec.submit(_click_cashout, fr_p, game, 5000, dbg_p)
                             if still_p else None)
                    if still_b:
                        clk_b = _click_cashout(fr_b, game, 5000, dbg_b)
                    if p_fut is not None:
                        clk_p = p_fut.result()
                    time.sleep(1.2)
                    p_fut = player_exec.submit(_cashout_ready, fr_p, game)
                    now_b = _cashout_ready(fr_b, game) if still_b else False
                    now_p = p_fut.result() if still_p else False
                    click_attempts.append({
                        "attempt": attempt_no,
                        "banker": ({"clicked": clk_b, **dbg_b} if still_b
                                   else {"skipped": "already_closed"}),
                        "player": ({"clicked": clk_p, **dbg_p} if still_p
                                   else {"skipped": "already_closed"}),
                        "still_open_after": {"banker": now_b, "player": now_p},
                    })
                    still_b, still_p = now_b, now_p
                    if still_b or still_p:
                        time.sleep(max(0.0, CASHOUT_CLICK_INTERVAL - 1.2))

                click_diag = {"attempts": click_attempts}
                summary.setdefault("cashout_click_diag", []).append(
                    {"round": rnd, "diag": click_diag})

                if still_b and still_p:
                    # NEITHER side cashed out. That is a failure worth stopping
                    # for, but it is NOT an exposure: both accounts still hold
                    # equal, opposite positions on the same round, so they
                    # remain hedged and will settle against each other. Saying
                    # "unhedged" here (as an earlier version did) is both wrong
                    # and alarming, so the two cases are reported separately.
                    summary["stop_reason"] = "cashout_failed"
                    summary["messages"].append(
                        f"Round {rnd}: neither side cashed out (click diagnostics: "
                        f"{click_diag}) — ₹{port_b} "
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
            # A multi-chip stake ends the round with the SMALLEST chip selected
            # (the plan's last group), so put the largest one back now, between
            # rounds, where there's no window pressure -- otherwise the next
            # round pays for that switch inside its own ~10s window. Purely an
            # optimisation: _place_stake re-selects anyway if this doesn't take.
            if len(stake_groups) > 1:
                first_chip = stake_groups[0][0]
                p_fut = player_exec.submit(select_chip_fast, fr_p, first_chip)
                select_chip_fast(fr_b, first_chip)
                p_fut.result()

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
    """Screenshot-saving is disabled globally (see save_screenshot()) -- a
    no-op that leaves summary["shots"] empty rather than recording paths
    nothing was ever written to."""
    return


def prompt_phone(site_url=None):
    """Ask for the phone number interactively. Normalised before it is
    returned, so a number typed with a country code is stored and reported as
    the number the form will actually carry -- see normalize_phone()."""
    while True:
        phone = input("Enter phone number for this signup: ").strip()
        if phone.isdigit() and 7 <= len(phone) <= 15:
            return normalize_phone(phone, site_url)
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

    # Default: generate a random identity, keep any explicit overrides. The
    # target site's profile shapes it -- username length and password policy
    # differ per site, and a value the form's own JS rejects never reaches
    # the server (see gen_account()).
    acct = gen_account(profile_for(args.url or SITE_URL))
    if args.username:
        acct["username"] = args.username
    if args.email:
        acct["email"] = args.email
    if args.password:
        acct["password"] = args.password
    acct["phone"] = (normalize_phone(args.phone, args.url) if args.phone
                     else prompt_phone(args.url))
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
    started = time.time()
    try:
        proxy_conf = parse_proxy(acct.get("proxy"))
        proxy_conf, bridge_proc = maybe_bridge_proxy(proxy_conf)
    except (ValueError, RuntimeError) as e:
        return {"account": acct.get("username", "?"), "ok": False,
                "messages": [str(e)], "shot": None}

    # Always go through a context now (rather than browser.new_page() when
    # there's no proxy): the target site may require a user agent, which can
    # only be set at context level.
    context = new_site_context(browser, acct.get("url"), proxy_conf)
    page = context.new_page()
    res = None
    try:
        res = signup_once(page, acct, submit=not args.no_submit,
                          interactive=not args.account_file,
                          site_url=acct.get("url"), proxy=acct.get("proxy"),
                          free_number=args.free_number,
                          # the BRIDGED conf, not the raw string -- see
                          # _context_proxy_conf()
                          proxy_conf=proxy_conf)
    except PWTimeout as e:
        res = {"account": acct.get("username", "?"), "ok": False,
               "messages": [f"Timeout: {str(e)[:120]}"], "shot": None}
    except PWError as e:
        # e.g. a broken/unreachable proxy raises this, not PWTimeout.
        res = {"account": acct.get("username", "?"), "ok": False,
               "messages": [f"Browser error (check --proxy?): {str(e)[:200]}"], "shot": None}
    finally:
        if isinstance(res, dict):
            res["secs"] = time.time() - started
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
    ap.add_argument("--no-free-number", dest="free_number", action="store_false",
                    help="disable freeing the signup phone number (see --free-number below) -- "
                         "it's ON by default")
    ap.add_argument("--free-number", dest="free_number", action="store_true", default=True,
                    help="[default: on] right after a successful signup, swap the account onto "
                         "a random throwaway phone number (cricmatch247 only) so the real phone "
                         "number just used is freed up and can be entered again for the next "
                         "signup -- pass --no-free-number to turn this off")
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
                                       site_url=acct.get("url"), proxy=acct.get("proxy"),
                                       free_number=args.free_number)
            else:
                res = run_browser_account(browser, acct, args)

            results.append(res)
            # Per-account wall time, because "the signups are slow" is only
            # actionable once you can see whether it is the first one (which
            # pays for a WAF solve) or every one.
            took = f" [{res['secs']:.1f}s]" if res.get("secs") is not None else ""
            print(f"[{'OK ' if res['ok'] else 'FAIL' if res['ok'] is False else '?  '}] "
                  f"{res['account']}{took}: {' | '.join(res['messages'])}")
            if res["shot"]:
                print(f"       screenshot: {res['shot']}")

            status = "success" if res["ok"] else ("failed" if res["ok"] is False else "unknown")
            db.update_status(conn, row_id, status,
                              notes="; ".join(res["messages"])[:500], screenshot=res["shot"])
            if res.get("freed_phone"):
                db.update_freed_phone(conn, row_id, res["freed_phone"])
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
