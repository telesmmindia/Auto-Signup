"""One-off inspector: log into an EXISTING account and dump every element
whose text mentions "wallet" or contains a rupee amount, so the real
wallet-balance selector can be found and read_wallet_balance() in main.py
(currently a best-effort heuristic, not yet verified live) can be checked
against real DOM.

Read-only: only logs in and reads the page. Never places a bet, never
changes anything on the account. Does not print the password.

Usage: .venv/bin/python inspect_wallet.py <username> <password> [site_url]
"""
import sys

from playwright.sync_api import sync_playwright

import main as engine

if len(sys.argv) < 3:
    print(f"Usage: {sys.argv[0]} <username> <password> [site_url]")
    sys.exit(1)

username, password = sys.argv[1], sys.argv[2]
site_url = sys.argv[3] if len(sys.argv) > 3 else engine.SITE_URL

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page()

    outcome, msgs = engine.login(page, username, password, site_url=site_url)
    print("login outcome:", outcome, msgs)
    if outcome != "ok":
        browser.close()
        sys.exit(1)

    page.wait_for_timeout(1500)

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
    print(f"\n=== {len(nodes)} candidate wallet/₹ elements ===")
    for n in nodes:
        print(n)

    found = engine.read_wallet_balance(page)
    print(f"\nread_wallet_balance() currently returns: {found}")
    print("Compare that to the site's own header -- if it's wrong or None, "
          "use the candidates above to write a real selector.")

    engine.SHOTS_DIR.mkdir(exist_ok=True)
    shot = engine.SHOTS_DIR / f"{username}-wallet-inspect.png"
    page.screenshot(path=str(shot))
    print(f"\nScreenshot: {shot}")

    browser.close()
