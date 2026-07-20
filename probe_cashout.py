"""Find the element that actually handles the CASH OUT click.

Why this exists: [data-role="cash-out"] reports disabled=false,
opacity=1, pointerEvents=auto even while the button is visibly greyed out,
and force-clicking it does nothing (runs 3 and 4, 2026-07-20, both ended
`cashout_failed`). That mismatch says the role element is a CONTAINER and the
real click target is elsewhere -- the same trap as the "REAL CHIPS" label and
the baccarat bet spots, both of which needed an inner element.

This dumps the full subtree, the computed cursor/pointer-events of every node
(cursor:pointer is the usual marker of the real button), and what
document.elementFromPoint() actually returns at the panel's centre -- which is
the ground truth for "what would a click land on".

Read-only: places NO bets. Run it again with --with-position once a real
position is open if the disabled-state dump isn't conclusive.

Usage: .venv/bin/python probe_cashout.py <username> <password>
"""
import argparse
import json
import time

from playwright.sync_api import sync_playwright

import main as m
from sites.games import STOCKMARKET

parser = argparse.ArgumentParser()
parser.add_argument("username")
parser.add_argument("password")
parser.add_argument("--url", default=None)
parser.add_argument("--secs", type=int, default=60)
args = parser.parse_args()

SITE_URL = args.url or m.SITE_URL

# Walk the cash-out panel's whole subtree. cursor:pointer and a click-ish
# tag/role are what mark the element the UI actually treats as the button.
_DUMP_SUBTREE_JS = """(role) => {
    const root = document.querySelector(`[data-role="${role}"]`);
    if (!root) return {error: "role not found"};
    const describe = (e, depth) => {
        const cs = getComputedStyle(e);
        const r = e.getBoundingClientRect();
        return {
            depth,
            tag: e.tagName,
            role: e.getAttribute('data-role') || '',
            cls: (e.className || '').toString().slice(0, 45),
            text: (e.innerText || '').replace(/\\n/g, ' | ').trim().slice(0, 40),
            cursor: cs.cursor,
            pointer: cs.pointerEvents,
            opacity: cs.opacity,
            bg: cs.backgroundColor,
            disabled: e.disabled === true || e.getAttribute('aria-disabled') === 'true',
            w: Math.round(r.width), h: Math.round(r.height),
            x: Math.round(r.left), y: Math.round(r.top),
        };
    };
    const out = [];
    const walk = (e, d) => {
        out.push(describe(e, d));
        for (const c of e.children) walk(c, d + 1);
    };
    walk(root, 0);

    // Ground truth: what would a click at the panel's centre actually hit?
    const rr = root.getBoundingClientRect();
    const cx = Math.round(rr.left + rr.width / 2);
    const cy = Math.round(rr.top + rr.height / 2);
    const hit = document.elementFromPoint(cx, cy);
    const chain = [];
    let n = hit;
    while (n && chain.length < 8) {
        const cs = getComputedStyle(n);
        chain.push({tag: n.tagName, role: n.getAttribute('data-role') || '',
                    cls: (n.className || '').toString().slice(0, 45),
                    cursor: cs.cursor, pointer: cs.pointerEvents});
        n = n.parentElement;
    }
    return {nodes: out, clickPoint: {x: cx, y: cy}, hitChain: chain,
            rootHTML: root.outerHTML.slice(0, 1500)};
}"""


def open_stock(context, page, username, password):
    outcome, _ = m.login(page, username, password, site_url=SITE_URL)
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
    return gp, fr


with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    context = browser.new_context()
    page = context.new_page()
    try:
        print(f"--- opening Stock Market as {args.username!r} ---")
        gp, fr = open_stock(context, page, args.username, args.password)
        print("    table id:", m._table_id(gp))

        print(f"\n--- sampling the CASH OUT panel across {args.secs}s ---")
        seen = set()
        deadline = time.time() + args.secs
        while time.time() < deadline:
            phase = m._read_instruction(fr)
            key = (phase or "").split("\n")[0]
            if key not in seen:
                seen.add(key)
                print(f"\n{'=' * 70}\nPHASE: {phase!r}   portfolio="
                      f"{m.read_portfolio(fr, STOCKMARKET)}\n{'=' * 70}")
                d = fr.evaluate(_DUMP_SUBTREE_JS, STOCKMARKET.cashout_role)
                if d.get("error"):
                    print("   ", d["error"])
                else:
                    print("  SUBTREE (indent = depth):")
                    for n in d["nodes"]:
                        pad = "    " + "  " * n["depth"]
                        mark = "  <== cursor:pointer" if n["cursor"] == "pointer" else ""
                        print(f"{pad}{n['tag']:8} role={n['role']!r:20} "
                              f"cur={n['cursor']:8} ptr={n['pointer']:6} "
                              f"op={n['opacity']:4} {n['w']}x{n['h']} "
                              f"txt={n['text']!r}{mark}")
                    print(f"\n  CLICK AT {d['clickPoint']} WOULD HIT (innermost first):")
                    for h in d["hitChain"]:
                        print(f"    {h['tag']:8} role={h['role']!r:20} "
                              f"cur={h['cursor']:8} ptr={h['pointer']:6} cls={h['cls']!r}")
                    print(f"\n  outerHTML (truncated):\n    {d['rootHTML'][:900]}")
            time.sleep(1)
    finally:
        try:
            context.close()
        except Exception:
            pass
        browser.close()
