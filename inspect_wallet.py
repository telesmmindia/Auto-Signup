"""One-off inspector: log into an EXISTING account, wait for
read_wallet_balance() (main.py) to resolve, and dump every element whose
text mentions "wallet" or contains a rupee amount -- for checking that
function's selector (sites/cricmatch.py's sel["wallet_balance"]) still
matches real DOM if the site's markup ever changes.

Read-only: only logs in and reads the page. Never places a bet, never
changes anything on the account. Does not print the password.

Usage: .venv/bin/python inspect_wallet.py <username> <password> [site_url] [--proxy <proxy>]
"""
import sys

from playwright.sync_api import sync_playwright

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

        outcome, msgs = engine.login(page, username, password, site_url=site_url)
        print("login outcome:", outcome, msgs)
        if outcome != "ok":
            sys.exit(1)

        # read_wallet_balance() itself polls (the balance spans are empty in
        # the raw HTML until the site's own onload getBalance() call fills
        # them in, ~20s observed live) -- wait for that here too before
        # scanning, or the DOM dump below just shows the pre-load emptiness.
        found = engine.read_wallet_balance(page)
        print(f"\nread_wallet_balance() returns: {found}")

        nodes = page.eval_on_selector_all(
            "body *",
            """els => els.filter(e => {
                const t = (e.innerText || '').trim();
                return t && t.length < 60 && (/wallet/i.test(t) || /₹/.test(t));
            }).slice(0, 50).map(e => ({
                tag: e.tagName,
                id: e.id || '',
                cls: e.className || '',
                text: (e.innerText || '').trim()
            }))"""
        )
        print(f"\n=== {len(nodes)} candidate wallet/₹ elements (for cross-checking) ===")
        for n in nodes:
            print(n)

        engine.SHOTS_DIR.mkdir(exist_ok=True)
        shot = engine.SHOTS_DIR / f"{username}-wallet-inspect.png"
        page.screenshot(path=str(shot))
        print(f"\nScreenshot: {shot}")
    finally:
        engine.stop_bridge(bridge_proc)
        browser.close()
