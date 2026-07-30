"""One-off read-only probe: drive a REAL login through Playwright while
capturing every XHR/fetch request+response, to find the site's actual
login POST endpoint and the getBalance() call the site fires on page load.
Goal: replace the slow full-browser login+poll in balance_checker.py with a
plain `requests` call, the same way http_register_call() replaced the
browser signup path.

Never places a bet, never changes account state beyond a normal login.
Does not print the password.

Usage: .venv/bin/python probe_login_balance.py <username> <password> [site_url] [--proxy <proxy>]
"""
import json
import sys

import main as engine

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

captured = []


def on_request(req):
    if req.resource_type in ("xhr", "fetch") or req.method == "POST":
        entry = {"phase": "request", "method": req.method, "url": req.url,
                  "headers": dict(req.headers), "post_data": req.post_data}
        captured.append(entry)


def on_response(resp):
    req = resp.request
    if req.resource_type in ("xhr", "fetch") or req.method == "POST":
        try:
            body = resp.text()
        except Exception:
            body = "<binary or unavailable>"
        entry = {"phase": "response", "status": resp.status, "url": resp.url,
                  "headers": dict(resp.headers), "body": body[:2000]}
        captured.append(entry)


from playwright.sync_api import sync_playwright

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
        page.on("request", on_request)
        page.on("response", on_response)

        print("Cookies BEFORE login:")
        for c in context.cookies():
            print(" ", c["name"], "=", (c["value"][:30] + "...") if len(c["value"]) > 30 else c["value"])

        outcome, msgs = engine.login(page, username, password, site_url=site_url)
        print("\nlogin outcome:", outcome, msgs)

        if outcome == "ok":
            print("\nWaiting up to 25s for balance to populate (getBalance call)...")
            found = engine.read_wallet_balance(page, timeout_secs=25)
            print("read_wallet_balance() ->", found)

        print("\nCookies AFTER login:")
        for c in context.cookies():
            print(" ", c["name"], "=", (c["value"][:30] + "...") if len(c["value"]) > 30 else c["value"])

        out_path = "probe_login_balance_capture.json"
        with open(out_path, "w") as f:
            json.dump(captured, f, indent=2)
        print(f"\nCaptured {len(captured)} request/response entries -> {out_path}")
    finally:
        engine.stop_bridge(bridge_proc)
        browser.close()
