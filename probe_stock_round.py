"""Watch a full Stock Market Live round, read-only.

Phase 0 follow-up. probe_evo_lobby.py established the route and the role
names; what's still unknown is the *timing* signal. Confirmed live
2026-07-20: this game has no [data-role="circle-timer"], so baccarat's
_betting_open() returns False forever here, and the visible ROLE SET does not
change across phases either -- so role diffing can't detect the betting
window. The phase lives in the TEXT of [data-role="instruction-message"]
("PLACE YOUR BETS", "MAKE YOUR DECISION", ...), which is what this samples.

Also captures, per sample: portfolio value, cash-out button state, total bet,
balance, and the chip rail -- everything the hedge round loop needs to gate
on. Places no bets.

Usage: .venv/bin/python probe_stock_round.py <username> <password> [--secs N]
"""
import argparse
import json
import time

from playwright.sync_api import sync_playwright

import main as m

parser = argparse.ArgumentParser()
parser.add_argument("username")
parser.add_argument("password")
parser.add_argument("--url", default=None)
parser.add_argument("--secs", type=int, default=180)
parser.add_argument("--headed", action="store_true")
args = parser.parse_args()

SITE_URL = args.url or m.SITE_URL

_TEXTS_JS = """() => {
    const vis = e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
    const seen = new Set();
    for (const e of Array.from(document.querySelectorAll('div,span,a,p'))) {
        if (!vis(e)) continue;
        const t = (e.innerText || '').trim();
        if (t && t.length < 34 && !t.includes('\\n')) seen.add(t);
    }
    return Array.from(seen).slice(0, 400);
}"""

# One sample of everything the round loop will need to gate on.
_SAMPLE_JS = """() => {
    const g = r => document.querySelector(`[data-role="${r}"]`);
    const txt = e => e ? (e.innerText || '').replace(/[\\u2066\\u2069\\u200b]/g, '').trim() : null;
    const box = e => { if (!e) return null; const r = e.getBoundingClientRect();
                       return {w: Math.round(r.width), h: Math.round(r.height)}; };
    const co = g('cash-out');
    const style = co ? getComputedStyle(co) : null;
    return {
        instruction: txt(g('instruction-message')),
        progress: txt(g('instruction-progress')),
        portfolio: txt(g('portfolio')),
        fee: txt(g('fee')),
        totalBet: txt(g('total-bet-label-value')),
        balance: txt(g('balance-label-value')),
        up: txt(g('SM_Up')),
        down: txt(g('SM_Down')),
        upBox: box(g('SM_Up')),
        cashOut: {
            present: !!co, text: txt(co), box: box(co),
            disabled: co ? (co.disabled === true ||
                            co.getAttribute('aria-disabled') === 'true') : null,
            opacity: style ? style.opacity : null,
            pointer: style ? style.pointerEvents : null,
            cls: co ? (co.className || '').toString().slice(0, 50) : null,
        },
        chips: Array.from(document.querySelectorAll('[data-role="chip-value"]'))
                    .map(e => txt(e)),
        selectedChip: txt(g('selected-chip')),
    };
}"""


def open_stock_market(context, page):
    """The route established live 2026-07-20 -- see probe_evo_lobby.py."""
    print("--- lobby ---", m.open_casino_lobby(page, timeout_ms=20000))
    gp = None
    for attempt in range(1, 4):
        m._dismiss_casino_promo_modal(page)
        before = list(context.pages)
        m.search_and_open_game(page, "Baccarat", "Baccarat A")
        gp = next((x for x in context.pages
                   if x not in before and ("evo-games" in x.url or "ezugi" in x.url)), None)
        if gp is None and ("evo-games" in page.url or "ezugi" in page.url):
            gp = page
        if gp is not None:
            break
        page.wait_for_timeout(2500)
        m.open_casino_lobby(page, timeout_ms=15000)
    if gp is None:
        raise SystemExit("could not open an Evolution game")
    print("    entered via Baccarat A")

    fr = m.find_game_frame(gp, "evo-games.com", timeout_ms=30000)
    if fr is None:
        raise SystemExit("no game frame")
    gp.wait_for_timeout(9000)

    fr.locator('[data-role="lobby-button"]').first.click(timeout=6000, force=True)
    gp.wait_for_timeout(6000)

    # The lobby is a SEPARATE iframe (url contains "?iFrAmE=x") -- the game
    # frame has no search box at all.
    lobby = None
    for f in gp.frames:
        try:
            t = f.evaluate(_TEXTS_JS)
            if any(x in ("For You", "Top Games", "Game Shows") for x in t):
                lobby = f
                break
        except Exception:
            continue
    if lobby is None:
        raise SystemExit("lobby frame not found")
    print("    lobby frame open")

    box = lobby.get_by_placeholder("Search").first
    box.click(timeout=5000)
    box.type("Stock", delay=140)
    gp.wait_for_timeout(4500)
    lobby.locator("text=Stock Market").first.click(timeout=6000, force=True)
    gp.wait_for_timeout(14000)
    print("    url:", gp.url[:130])

    for f in gp.frames:
        try:
            if f.evaluate('() => !!document.querySelector(\'[data-role="SM_Up"]\')'):
                return gp, f
        except Exception:
            continue
    raise SystemExit("Stock Market frame not found")


with sync_playwright() as p:
    browser = p.chromium.launch(headless=not args.headed, slow_mo=100)
    context = browser.new_context()
    page = context.new_page()
    try:
        print(f"--- login {args.username!r} ---")
        outcome, _ = m.login(page, args.username, args.password, site_url=SITE_URL)
        print("    outcome:", outcome)
        if outcome != "ok":
            raise SystemExit(1)

        gp, fr = open_stock_market(context, page)
        print("    stock market frame:", fr.url[:110])
        gp.screenshot(path="shots/stock-round-start.png")

        print(f"\n=== SAMPLING {args.secs}s ===")
        prev_instr = object()
        deadline = time.time() + args.secs
        while time.time() < deadline:
            try:
                s = fr.evaluate(_SAMPLE_JS)
            except Exception as e:
                print("  sample failed:", str(e)[:70])
                time.sleep(2)
                continue
            stamp = time.strftime("%H:%M:%S")
            if s["instruction"] != prev_instr:
                print(f"\n  [{stamp}] PHASE -> {s['instruction']!r} "
                      f"(progress={s['progress']!r})")
                print(f"           cashOut={json.dumps(s['cashOut'])}")
                print(f"           chips={s['chips']} selected={s['selectedChip']!r}")
                prev_instr = s["instruction"]
            else:
                print(f"  [{stamp}] instr={s['instruction']!r} portfolio={s['portfolio']!r} "
                      f"totalBet={s['totalBet']!r} bal={s['balance']!r} "
                      f"coDisabled={s['cashOut']['disabled']} "
                      f"coOpacity={s['cashOut']['opacity']}")
            time.sleep(2)
        gp.screenshot(path="shots/stock-round-end.png")
    finally:
        try:
            context.close()
        except Exception:
            pass
        browser.close()
