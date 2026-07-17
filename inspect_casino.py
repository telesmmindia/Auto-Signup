"""One-off inspector: log in, find the casino section, search "baccarat",
open the game, and dump what's actually there (DOM fields / iframes / canvas)
so real selectors can be written for login() / casino nav / bet placement.

Nothing here is wired into main.py yet -- this is discovery only, run it
headed and read the printed output + screenshots before writing any
production code against a guessed selector.

Usage: python inspect_casino.py <url> <username> <password> [game_query]
  (game_query defaults to "baccarat")
"""
import sys

from playwright.sync_api import sync_playwright

if len(sys.argv) < 4:
    print(__doc__)
    sys.exit(1)

URL = sys.argv[1]
USERNAME = sys.argv[2]
PASSWORD = sys.argv[3]
GAME_QUERY = sys.argv[4] if len(sys.argv) > 4 else "baccarat"

CLOSE_POPUP_SEL = [".mnPopupClose", ".pgSoftClsBtn", ".support_popup_close",
                   ".areSurecancelBtn", "button:has-text('Close')",
                   ".skip_right_img"]


def dump_visible_fields(page, label):
    fields = page.eval_on_selector_all(
        "input, select, button, a",
        """els => els.map(e => ({
            tag: e.tagName,
            type: e.type || '',
            name: e.name || '',
            id: e.id || '',
            placeholder: e.placeholder || '',
            cls: e.className || '',
            text: (e.innerText||'').trim().slice(0,40),
            visible: !!(e.offsetWidth || e.offsetHeight)
        }))"""
    )
    print(f"\n=== VISIBLE FIELDS: {label} ===")
    for f in fields:
        if f["visible"] and (f["tag"] in ("INPUT", "SELECT") or f["text"]):
            print(f)


def dump_login_candidates(page):
    print("\n=== LOOKING FOR A LOGIN TRIGGER ===")
    for sel in ["text=/login/i", "text=/sign in/i", "a:has-text('Login')",
                "button:has-text('Login')", "[class*=login]", "[class*=Login]"]:
        try:
            loc = page.locator(sel)
            n = loc.count()
            if n:
                print(f"{sel!r} -> {n} match(es)")
                for i in range(min(n, 5)):
                    item = loc.nth(i)
                    if item.is_visible():
                        print("  visible:", item.evaluate(
                            "e => ({tag:e.tagName, id:e.id, cls:e.className, text:(e.innerText||'').trim().slice(0,40)})"
                        ))
        except Exception as e:
            print(f"  miss {sel}: {str(e)[:60]}")


def dump_casino_candidates(page):
    print("\n=== LOOKING FOR A CASINO/GAMES ENTRY POINT ===")
    for sel in ["text=/casino/i", "text=/games/i", "a:has-text('Casino')",
                "[class*=casino]", "[class*=Casino]"]:
        try:
            loc = page.locator(sel)
            n = loc.count()
            if n:
                print(f"{sel!r} -> {n} match(es)")
                for i in range(min(n, 5)):
                    item = loc.nth(i)
                    if item.is_visible():
                        print("  visible:", item.evaluate(
                            "e => ({tag:e.tagName, id:e.id, cls:e.className, text:(e.innerText||'').trim().slice(0,40)})"
                        ))
        except Exception as e:
            print(f"  miss {sel}: {str(e)[:60]}")


def dump_frames(page):
    print(f"\n=== FRAMES ON PAGE (url={page.url}) ===")
    for fr in page.frames:
        print(f"  frame url={fr.url!r} name={fr.name!r}")


with sync_playwright() as p:
    browser = p.chromium.launch(headless=False, slow_mo=150)
    page = browser.new_page()
    page.goto(URL, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(5000)

    print("--- Step 1: dismiss popups ---")
    for sel in CLOSE_POPUP_SEL:
        try:
            loc = page.locator(sel)
            for i in range(loc.count()):
                item = loc.nth(i)
                if item.is_visible():
                    item.click(timeout=1500)
                    print(f"closed popup via {sel}")
        except Exception:
            pass
    page.wait_for_timeout(1000)

    print("--- Step 2: find + click login ---")
    dump_login_candidates(page)
    print("\n>>> Manually note which selector above is the real LOGIN trigger.")
    print(">>> This script does NOT auto-click it yet -- inspect first, wire up next.")

    page.screenshot(path="shots/inspect-casino-initial.png", full_page=False)

    input("\nPress Enter after you've manually clicked LOGIN in the open browser window "
          "(or Ctrl+C to stop here) ...")

    dump_visible_fields(page, "after manual login click (should show login form)")
    page.screenshot(path="shots/inspect-casino-login-form.png", full_page=False)

    input(f"\nPress Enter after you've manually logged in as {USERNAME!r} "
          "in the open browser window ...")

    dump_visible_fields(page, "after manual login (should show balance/account widget)")
    page.screenshot(path="shots/inspect-casino-logged-in.png", full_page=False)

    print("--- Step 3: find casino/games entry point ---")
    dump_casino_candidates(page)

    input(f"\nPress Enter after you've manually navigated to the casino section "
          f"and searched for {GAME_QUERY!r} ...")

    dump_visible_fields(page, f"casino search results for {GAME_QUERY!r}")
    dump_frames(page)
    page.screenshot(path="shots/inspect-casino-search.png", full_page=False)

    input(f"\nPress Enter after you've manually opened the {GAME_QUERY} game ...")

    print("--- Step 4: inspect the opened game ---")
    dump_frames(page)
    for fr in page.frames:
        try:
            fields = fr.eval_on_selector_all(
                "input, button, a, canvas",
                """els => els.map(e => ({
                    tag: e.tagName, id: e.id || '', cls: e.className || '',
                    text: (e.innerText||'').trim().slice(0,40)
                }))"""
            )
            print(f"\n  frame {fr.url!r}: {len(fields)} input/button/a/canvas nodes")
            canvas_only = fields and all(f["tag"] == "CANVAS" for f in fields)
            if canvas_only:
                print("  *** THIS FRAME IS CANVAS-ONLY -- no clickable DOM bet spots. ***")
            for f in fields[:30]:
                print("   ", f)
        except Exception as e:
            print(f"  frame {fr.url!r}: could not inspect ({str(e)[:80]})")

    page.screenshot(path="shots/inspect-casino-game.png", full_page=True)

    print("\nDone. Review the printed frame/field dump above and the shots/inspect-casino-*.png "
          "screenshots to decide: DOM selectors vs coordinate-based clicks.")
    input("Press Enter to close the browser ...")
    browser.close()
