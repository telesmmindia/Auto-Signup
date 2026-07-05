"""One-off inspector: open the site, click JOIN, dump the signup modal's fields."""
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page()
    page.goto("https://cricmatch247.com/", wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(5000)

    # Dismiss any promo / support popups first
    for sel in [".mnPopupClose", ".pgSoftClsBtn", ".support_popup_close", ".cancel_btn",
                "button:has-text('Close')", "[class*=close]"]:
        try:
            for i in range(page.locator(sel).count()):
                loc = page.locator(sel).nth(i)
                if loc.is_visible():
                    loc.click(timeout=1500)
                    print(f"closed popup via {sel}")
        except Exception:
            pass
    page.wait_for_timeout(1500)

    # Click the JOIN / register button (force past any overlay)
    clicked = False
    for sel in ["button.headerjoinBtn", "button.cls_reg_btn", ".join__btn",
                ".registerUserData", "text=JOIN"]:
        try:
            page.locator(sel).first.click(timeout=4000, force=True)
            clicked = True
            print(f"Clicked: {sel}")
            break
        except Exception as e:
            print(f"  miss {sel}: {str(e)[:60]}")
    print("clicked:", clicked)
    page.wait_for_timeout(4000)

    fields = page.eval_on_selector_all(
        "input, select, button[type=submit], button",
        """els => els.map(e => ({
            tag: e.tagName,
            type: e.type || '',
            name: e.name || '',
            id: e.id || '',
            placeholder: e.placeholder || '',
            cls: e.className || '',
            text: (e.innerText||'').trim().slice(0,30),
            visible: !!(e.offsetWidth || e.offsetHeight)
        }))"""
    )
    print("\n=== VISIBLE FIELDS AFTER CLICK ===")
    for f in fields:
        if f["visible"] and (f["tag"] in ("INPUT", "SELECT") or f["type"] == "submit"):
            print(f)
    print("\n=== VISIBLE BUTTONS (text) ===")
    for f in fields:
        if f["visible"] and f["tag"] == "BUTTON" and f["text"]:
            print(f["text"], "|", f["cls"][:50])

    page.screenshot(path="signup_modal.png", full_page=False)
    browser.close()
