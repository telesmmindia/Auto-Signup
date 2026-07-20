"""Find how to select a chip denomination on Stock Market Live.

Why: the engine never picks a chip, so a bet always lands at whatever the
table has pre-selected (₹10, the minimum). Asking for ₹100 therefore places
₹10 and trips the amount_mismatch safety stop. To support real amounts we
need to identify which chip is which and click the right one.

The catch found earlier: [data-role="chip-value"] elements return an empty
innerText -- the numbers are rendered as SVG, not text -- so the value has to
come from somewhere else (an attribute, an aria-label, the SVG's own <text>,
or the chip's ordering). This dumps every candidate so we can pick a reliable
one instead of guessing.

Read-only: selects nothing, bets nothing.

Usage: .venv/bin/python probe_chips.py <username> <password>
"""
import argparse
import json

from playwright.sync_api import sync_playwright

import main as m
from sites.games import STOCKMARKET

parser = argparse.ArgumentParser()
parser.add_argument("username")
parser.add_argument("password")
parser.add_argument("--url", default=None)
args = parser.parse_args()

SITE_URL = args.url or m.SITE_URL

_DUMP_CHIPS_JS = """() => {
    const info = e => {
        const r = e.getBoundingClientRect();
        const attrs = {};
        for (const a of e.attributes) attrs[a.name] = a.value.slice(0, 60);
        // SVG numbers live in <text> nodes, which innerText misses.
        const svgText = Array.from(e.querySelectorAll('text'))
            .map(t => (t.textContent || '').trim()).filter(Boolean);
        return {
            tag: e.tagName,
            role: e.getAttribute('data-role') || '',
            text: (e.innerText || '').trim().slice(0, 20),
            textContent: (e.textContent || '').trim().slice(0, 20),
            svgText,
            aria: e.getAttribute('aria-label') || '',
            title: e.getAttribute('title') || '',
            attrs,
            cls: (e.className || '').toString().slice(0, 40),
            w: Math.round(r.width), h: Math.round(r.height),
            x: Math.round(r.left), y: Math.round(r.top),
            cursor: getComputedStyle(e).cursor,
        };
    };
    const out = {};
    for (const role of ['chip', 'chip-value', 'selected-chip', 'chip-stack',
                        'chip-stack-wrapper', 'double-button', 'undo-button',
                        'expanded-chip-stack-wrapper']) {
        out[role] = Array.from(document.querySelectorAll(`[data-role="${role}"]`)).map(info);
    }
    return out;
}"""


with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    context = browser.new_context()
    page = context.new_page()
    try:
        print(f"--- opening Stock Market as {args.username!r} ---")
        outcome, _ = m.login(page, args.username, args.password, site_url=SITE_URL)
        print("    login:", outcome)
        if outcome != "ok":
            raise SystemExit(1)
        m.open_casino_lobby(page, timeout_ms=20000)
        gp = None
        for _ in range(3):
            m._dismiss_casino_promo_modal(page)
            before = list(context.pages)
            m.search_and_open_game(page, "Baccarat", "Baccarat A")
            gp = next((x for x in context.pages
                       if x not in before and ("evo-games" in x.url or "ezugi" in x.url)), None)
            if gp is None and ("evo-games" in page.url or "ezugi" in page.url):
                gp = page
            if gp:
                break
            page.wait_for_timeout(2000)
            m.open_casino_lobby(page, timeout_ms=15000)
        if not gp:
            raise SystemExit("could not open an Evolution game")
        fr = m.find_game_frame(gp, "evo-games.com", timeout_ms=30000)
        fr = m._open_via_provider_lobby(gp, fr, STOCKMARKET)
        m.wait_for_live_table(fr, gp, game=STOCKMARKET)
        gp.wait_for_timeout(3000)

        data = fr.evaluate(_DUMP_CHIPS_JS)
        for role, items in data.items():
            print(f"\n=== [data-role=\"{role}\"] -- {len(items)} element(s) ===")
            for it in items[:3]:
                print("   ", json.dumps(it, ensure_ascii=False))
        gp.screenshot(path="shots/probe-chips.png")

        # The real question after run #8: does clicking a chip actually WORK,
        # and does it depend on the phase? Try selecting 100 repeatedly across
        # a couple of full cycles and report per-phase.
        import time as _t
        print("\n=== can we select the 100 chip, and when? ===")
        end = _t.time() + 120
        last = None
        while _t.time() < end:
            phase = (m._read_instruction(fr) or "?").split("\n")[0]
            rail = m.read_chips(fr)
            if rail.get("selected") != 100:
                try:
                    loc = fr.locator('[data-role="chip"][data-value="100"]')
                    n = loc.count()
                    loc.first.click(timeout=3000, force=True)
                    _t.sleep(1.2)
                except Exception as e:
                    n = -1
                after = m.read_chips(fr).get("selected")
                print(f"  [{_t.strftime('%H:%M:%S')}] phase={phase!r:22} "
                      f"matches={n} selected {rail.get('selected')} -> {after}"
                      f"   {'WORKED' if after == 100 else 'ignored'}")
                if after == 100:
                    print("  ...now selecting 10 again to retest")
                    try:
                        fr.locator('[data-role="chip"][data-value="10"]').first.click(
                            timeout=3000, force=True)
                    except Exception:
                        pass
                    _t.sleep(1.2)
            _t.sleep(2)
    finally:
        try:
            context.close()
        except Exception:
            pass
        browser.close()
