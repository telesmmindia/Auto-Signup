"""One-off inspector: log into an EXISTING account and look for a
self-service "change password" mechanism on the Account Details/Settings
page -- neither the DOM nor any network traffic for this has ever been
captured before (unlike wallet balance / free-number, which both have
confirmed selectors/endpoints). This script determines which of three cases
applies:

  A) a visible DOM form (current/new/confirm password fields + submit)
  B) an AJAX-only action (no visible form, but a password-related request
     fires from some page JS)
  C) nothing at all (mirrors the already-documented finding that this site's
     Account Details page has no self-service "change mobile number" UI)

Read-only: it clicks around navigation/tabs to reveal a possible change-
password panel, but it NEVER types into a password field and NEVER clicks
any submit-labeled button attached to one. Does not print the account's own
login password back.

Usage: .venv/bin/python inspect_account_settings.py <username> <password> [site_url] [--proxy <proxy>]
"""
import re
import sys

from playwright.sync_api import sync_playwright

import main as engine

# Keywords used to flag both DOM elements and network requests as
# possibly password-related, without assuming any particular site convention.
KEYWORD_RE = re.compile(r"password|security", re.I)
# Same-origin only (cricmatch247.com), not CDN asset paths, which happen to
# contain "accounts"/"account" as folder names and drown out real signal.
URL_KEYWORD_RE = re.compile(
    r"cricmatch247\.com.*(password|profile|account|settings|security)", re.I)

argv = sys.argv[1:]
proxy = None
if "--proxy" in argv:
    idx = argv.index("--proxy")
    proxy = argv[idx + 1] if idx + 1 < len(argv) else None
    del argv[idx:idx + 2]

if len(argv) < 2:
    print(f"Usage: {sys.argv[0]} <username> <password> [site_url] [--proxy <proxy>]")
    sys.exit(1)

username, password = argv[0], argv[1]
site_url = argv[2] if len(argv) > 2 else engine.SITE_URL

captured_requests = []


def _on_request(req):
    if URL_KEYWORD_RE.search(req.url):
        captured_requests.append(f"-> {req.method} {req.url}")


def _on_response(resp):
    if URL_KEYWORD_RE.search(resp.url):
        try:
            body = resp.text()[:300]
        except Exception:
            body = "<unreadable body>"
        captured_requests.append(f"<- {resp.status} {resp.url}\n     body: {body}")


def dump_password_related_elements(page, label):
    nodes = page.eval_on_selector_all(
        "body *",
        """els => els.filter(e => {
            const t = (e.innerText || '').trim();
            const idcls = (e.id || '') + ' ' + (e.className || '');
            return (e.tagName === 'INPUT' && e.type === 'password')
                || /password|security/i.test(t.length < 80 ? t : '')
                || /password|security/i.test(idcls);
        }).slice(0, 60).map(e => ({
            tag: e.tagName,
            id: e.id || '',
            cls: (typeof e.className === 'string') ? e.className : '',
            name: e.getAttribute('name') || '',
            type: e.getAttribute('type') || '',
            placeholder: e.getAttribute('placeholder') || '',
            text: (e.innerText || '').trim().slice(0, 80),
            visible: !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length)
        }))"""
    )
    print(f"\n=== {label}: {len(nodes)} password/security-related DOM elements ===")
    for n in nodes:
        print(n)
    return nodes


with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    bridge_proc = None
    try:
        if proxy:
            proxy_conf = engine.parse_proxy(proxy)
            proxy_conf, bridge_proc = engine.maybe_bridge_proxy(proxy_conf)
            context = browser.new_context(proxy=proxy_conf) if proxy_conf else browser.new_context()
        else:
            context = browser.new_context()
        page = context.new_page()
        page.on("request", _on_request)
        page.on("response", _on_response)

        outcome, msgs = engine.login(page, username, password, site_url=site_url)
        print("login outcome:", outcome, msgs)
        if outcome != "ok":
            sys.exit(1)

        prof = engine.profile_for(site_url or engine.SITE_URL)

        # Same "settle" margin free_phone_number() needed -- other widgets on
        # this site (wallet balance, account panel) hydrate via their own
        # background AJAX shortly after login, not instantly when the login
        # indicator itself appears. The site's own /login success handler
        # ALSO does its own `window.location.href = ...response.url` redirect
        # ~1s after a successful AJAX login -- give that time to land before
        # checking anything, and print where it actually lands.
        page.wait_for_timeout(8000)
        print(f"\nURL after login + 8s settle wait (before any reload of our own): {page.url}")

        # No known Account Details/Settings URL exists anywhere in the
        # codebase today -- get there the same way a real user would, via
        # the same #acctSec link login() already polls for as its success
        # indicator. force=True: cricmatch247 shows a full-page SPRIBE/Aviator
        # walkthrough overlay on load that intercepts plain clicks on header
        # nav (same trap documented for casino_nav in CLAUDE.md).
        page.locator(prof.sel["logged_in_indicator"]).first.click(timeout=5000, force=True)
        page.wait_for_timeout(2000)
        print(f"\nAccount Details URL: {page.url}")

        dump_password_related_elements(page, "initial page")

        try:
            panel_html = page.locator("#accountsections").first.inner_html(timeout=3000)
            print(f"\n=== #accountsections panel innerHTML ({len(panel_html)} chars) ===")
            print(panel_html[:4000])
        except Exception as e:
            print(f"\n(could not read #accountsections innerHTML: {str(e)[:150]})")

        # Look for a plausible "Change Password" tab/link/accordion that
        # might be collapsed, and click through each candidate to reveal it.
        # Read-only: this only expands panels/navigates, never fills/submits.
        candidates = page.eval_on_selector_all(
            "a, button, [role=tab], .accordion, .accordion-header, li",
            """els => els.filter(e => {
                const t = (e.innerText || '').trim();
                return t && t.length < 40 && /password|security/i.test(t);
            }).map(e => (e.innerText || '').trim())"""
        )
        candidates = list(dict.fromkeys(candidates))  # de-dupe, preserve order
        print(f"\n=== {len(candidates)} candidate nav/tab labels matching password/security/change ===")
        for c in candidates:
            print(repr(c))

        for label in candidates[:5]:
            try:
                page.locator(f"text={label}").first.click(timeout=3000)
                page.wait_for_timeout(1500)
                dump_password_related_elements(page, f"after clicking {label!r}")
            except Exception as e:
                print(f"(could not click {label!r}: {str(e)[:150]})")

        engine.SHOTS_DIR.mkdir(exist_ok=True)
        shot = engine.SHOTS_DIR / f"{username}-account-settings-inspect.png"
        page.screenshot(path=str(shot), full_page=True)
        print(f"\nScreenshot: {shot}")

        html_path = engine.SHOTS_DIR / f"{username}-account-settings.html"
        html_path.write_text(page.content())
        print(f"Full page source saved: {html_path} ({html_path.stat().st_size} bytes)")

        print(f"\n=== {len(captured_requests)} password/account/settings-keyword network events captured ===")
        for line in captured_requests:
            print(line)
    finally:
        engine.stop_bridge(bridge_proc)
        browser.close()
