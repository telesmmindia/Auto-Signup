"""SPENDS REAL MONEY: places ONE bet at the table minimum to work out how to
cash out.

Authorised by the site owner 2026-07-20 after three failed attempts to infer
this without a live position. Everything about the ENABLED CASH OUT button is
unobservable otherwise -- runs 3, 4 and 10 all failed because the panel looks
identical in the DOM whether it is live or dead, and the label-opacity theory
was disproved by run #10's trace (the position's value was visibly moving,
105.7 -> 138.8 -> 14.0, while the check still read "disabled").

Deliberately minimal exposure:
  * ONE account, so nothing else is affected
  * ONE bet, at the 10-rupee table minimum
  * ONE round -- it never re-bets, whatever happens
The stake can be lost, and at these swings most of it can vanish in seconds.
That is the accepted cost of the diagnosis.

What it does, in order:
  1. open Stock Market, select the 10 chip
  2. place a single UP bet, confirm it landed via TOTAL BET
  3. wait until the portfolio is provably MOVING (position live, not staged)
  4. dump the panel's full computed styling -- the state never seen before
  5. try click strategies in turn until the portfolio drops to 0
  6. report which one worked, and the balance before/after

Usage: .venv/bin/python probe_live_cashout.py <username> <password>
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
parser.add_argument("--amount", type=int, default=10, help="table minimum; keep it there")
parser.add_argument("--side", default="SM_Up")
args = parser.parse_args()

SITE_URL = args.url or m.SITE_URL

_DUMP_JS = """(role) => {
    const root = document.querySelector(`[data-role="${role}"]`);
    if (!root) return {error: "not found"};
    const nodes = [];
    const walk = (e, d) => {
        const cs = getComputedStyle(e);
        nodes.push({d, tag: e.tagName, cls: (e.className || '').toString().slice(0, 40),
                    txt: (e.innerText || '').trim().slice(0, 18),
                    op: cs.opacity, color: cs.color, bg: cs.backgroundColor,
                    cursor: cs.cursor, filter: cs.filter, pointer: cs.pointerEvents});
        for (const c of e.children) walk(c, d + 1);
    };
    walk(root, 0);
    return {nodes, html: root.outerHTML.slice(0, 1000)};
}"""


def show_dump(label, d):
    print(f"\n=== {label} ===")
    if d.get("error"):
        print("   ", d["error"])
        return
    for n in d["nodes"]:
        print(f"    {'  ' * n['d']}{n['tag']:7} op={n['op']:5} cur={n['cursor']:8} "
              f"ptr={n['pointer']:6} color={n['color']:22} bg={n['bg']:24} "
              f"filter={n['filter'][:18]:18} txt={n['txt']!r} cls={n['cls']!r}")
    print(f"\n    HTML: {d['html'][:700]}")


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

        bal0 = m.read_game_balance(fr)
        print(f"    balance before: ₹{bal0}")
        show_dump("BUTTON WITH NO POSITION (known dead state)",
                  fr.evaluate(_DUMP_JS, STOCKMARKET.cashout_role))

        print(f"\n--- selecting the ₹{args.amount} chip ---")
        if not m.select_chip(fr, args.amount):
            raise SystemExit("could not select the chip; no bet placed")
        print("    selected:", m.read_chips(fr))

        print(f"\n--- placing ONE ₹{args.amount} bet on {args.side} ---")
        placed = False
        deadline = time.time() + 180
        while time.time() < deadline and not placed:
            if not m._betting_open(fr, STOCKMARKET):
                time.sleep(0.5)
                continue
            if (m._read_total_bet(fr) or 0) != 0:
                time.sleep(0.5)
                continue
            m._click_bet_spot(fr, args.side)
            time.sleep(1.5)
            tb = m._read_total_bet(fr)
            print(f"    TOTAL BET now: {tb}")
            if tb:
                placed = True
        if not placed:
            raise SystemExit("no bet landed; nothing spent")

        print("\n--- waiting for the position to actually MOVE ---")
        staged = m.read_portfolio(fr, STOCKMARKET)
        print(f"    staged portfolio: {staged}")
        live = False
        deadline = time.time() + 90
        while time.time() < deadline:
            port = m.read_portfolio(fr, STOCKMARKET)
            if port and staged and abs(port - staged) > 0.01:
                print(f"    MOVING: portfolio {staged} -> {port}")
                live = True
                break
            time.sleep(0.25)
        if not live:
            print("    !! portfolio never moved; letting it settle, nothing more to try")
            raise SystemExit(2)

        show_dump("BUTTON WITH A LIVE POSITION (never seen before)",
                  fr.evaluate(_DUMP_JS, STOCKMARKET.cashout_role))
        print("    _cashout_enabled() currently says:",
              m._cashout_enabled(fr, STOCKMARKET))

        # The panel's look is painted on a CANVAS inside it (class ZUCfks),
        # which is why the DOM is byte-identical enabled vs disabled. Read the
        # canvas PIXELS for diagnosis, but don't gate clicking on either signal
        # -- _cashout_enabled (label opacity) was already disproved live (run
        # #10: portfolio moving 105.7->138.8->14.0 while it still read False),
        # so trusting it here would just repeat that mistake. Instead fire
        # _click_cashout on a timer through the whole ride and let the
        # PORTFOLIO readout itself say whether a click landed.
        _CANVAS_JS = """(role) => {
            const root = document.querySelector(`[data-role="${role}"]`);
            const c = root && root.querySelector('canvas');
            if (!c) return null;
            try {
                const ctx = c.getContext('2d');
                const w = c.width, h = c.height;
                const pts = [[w*0.5,h*0.5],[w*0.3,h*0.5],[w*0.7,h*0.5],[w*0.5,h*0.3]];
                let r=0,g=0,b=0,a=0;
                for (const [x,y] of pts) {
                    const d = ctx.getImageData(Math.round(x), Math.round(y), 1, 1).data;
                    r+=d[0]; g+=d[1]; b+=d[2]; a+=d[3];
                }
                const n = pts.length;
                return {r: Math.round(r/n), g: Math.round(g/n), b: Math.round(b/n),
                        a: Math.round(a/n)};
            } catch (e) { return {error: String(e).slice(0,60)}; }
        }"""

        print("\n--- riding the round, attempting CASH OUT every ~1.5s ---")
        print("    (gold button should show a high red+green, low blue)")
        attempts = []
        success = None
        seen = None
        last_click = 0.0
        CLICK_EVERY = 1.5
        end = time.time() + 75
        while time.time() < end and success is None:
            phase = (m._read_instruction(fr) or "").replace(chr(10), " ")
            port = m.read_portfolio(fr, STOCKMARKET)
            px = fr.evaluate(_CANVAS_JS, STOCKMARKET.cashout_role)
            key = (phase, px and (px.get("r"), px.get("g"), px.get("b")))
            if key != seen:
                print(f"    [{time.strftime('%H:%M:%S')}] phase={phase!r:24} "
                      f"portfolio={port!s:8} canvas={px}")
                seen = key

            settled = not phase or "NEXT GAME" in phase
            if port and port > 0 and not settled and time.time() - last_click >= CLICK_EVERY:
                last_click = time.time()
                clicked = m._click_cashout(fr, STOCKMARKET)
                time.sleep(1.0)
                port_after = m.read_portfolio(fr, STOCKMARKET)
                phase_after = (m._read_instruction(fr) or "").replace(chr(10), " ")
                landed = (clicked and port_after is not None and port_after < 0.01
                          and not ("NEXT GAME" in phase_after or not phase_after))
                rec = {"t": round(time.time() - end + 75, 1), "phase": phase,
                       "port_before": port, "clicked": clicked,
                       "port_after": port_after, "phase_after": phase_after,
                       "canvas": px, "landed": landed}
                attempts.append(rec)
                print(f"        attempt #{len(attempts)}: found={clicked} "
                      f"portfolio {port} -> {port_after}  phase_after={phase_after!r} "
                      f"{'*** LANDED ***' if landed else ''}")
                if landed:
                    success = rec
            time.sleep(0.4)

        time.sleep(2)
        bal1 = m.read_game_balance(fr)
        print(f"\n=== RESULT ===")
        print(f"    click attempts   : {len(attempts)}")
        if success:
            print(f"    CASH OUT WORKED at t={success['t']}s, phase={success['phase']!r}, "
                  f"canvas={success['canvas']}")
        else:
            print("    no click produced an immediate portfolio drop "
                  "(any drop to 0 happened only via natural round settlement)")
        print(f"    balance          : ₹{bal0} -> ₹{bal1}  ({(bal1 or 0) - (bal0 or 0):+})")
        print(f"    attempts (json)  : {json.dumps(attempts)}")
        gp.screenshot(path="shots/probe-live-cashout.png")
        print("    screenshot -> shots/probe-live-cashout.png")
    finally:
        try:
            context.close()
        except Exception:
            pass
        browser.close()
