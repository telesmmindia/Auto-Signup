"""One-off inspector for Evolution's "Stock Market Live" game.

Discovery only -- this places NO bets and clicks nothing inside the game
frame. Same precedent as inspect_casino.py: run it headed, read the printed
dump, and only then write production selectors. Baccarat's data-role
convention (bet-spot-Banker / circle-timer / balance-label-value) may or may
not carry over to this game, and guessing with real money is not acceptable.

What it answers (the Phase 0 unknowns):
  * every [data-role] present in the game frame, and how that set changes
    across a full round -- this is how we learn the betting-window and
    cash-out-window detectors
  * where UP / DOWN / CASH OUT / PORTFOLIO actually live in the DOM
  * whether TOTAL BET and the balance readout parse with the existing
    _read_total_bet / read_game_balance helpers
  * whether the chip rail (10 / 100 / x2 DOUBLE / UNDO / REPEAT) is real DOM

Login is automated; navigation to the game is left to you on purpose, since
which nav path is most reliable (site search vs category tab vs the
provider's own lobby search) is itself one of the open questions.

Usage:
  .venv/bin/python inspect_stockmarket.py <username> <password> [--url URL]
                                          [--proxy P] [--watch SECONDS]
"""
import argparse
import json
import sys
import time

from playwright.sync_api import sync_playwright

import main as m

parser = argparse.ArgumentParser(add_help=True)
parser.add_argument("username")
parser.add_argument("password")
parser.add_argument("--url", default=None, help="site URL (defaults to main.SITE_URL)")
parser.add_argument("--proxy", default=None, help="proxy string, same format as main.py")
parser.add_argument("--watch", type=int, default=120,
                    help="seconds to watch the live table (default 120, ~2 rounds)")
args = parser.parse_args()

SITE_URL = args.url or m.SITE_URL


# --- JS probes (all read-only) ------------------------------------------

# Enumerate every data-role in the frame with enough context to tell a real
# control from a hidden template. Baccarat taught us that hidden/oversized
# elements (the collapsed bet-limits tooltip) can carry the exact label text
# we're looking for, so size and visibility are dumped alongside every role.
_DUMP_ROLES_JS = """() => {
    return Array.from(document.querySelectorAll('[data-role]')).map(e => {
        const r = e.getBoundingClientRect();
        return {
            role: e.getAttribute('data-role'),
            tag: e.tagName,
            text: (e.innerText || '').replace(/[\\u2066\\u2069\\u200b]/g, '').trim().slice(0, 60),
            w: Math.round(r.width), h: Math.round(r.height),
            x: Math.round(r.left), y: Math.round(r.top),
            visible: r.width > 0 && r.height > 0,
        };
    });
}"""

# Just the role names, for cheap per-sample diffing during the watch loop.
_ROLE_NAMES_JS = """() => Array.from(
    new Set(Array.from(document.querySelectorAll('[data-role]'))
        .filter(e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; })
        .map(e => e.getAttribute('data-role')))).sort()"""

# Find the controls by text, but exclude the giant wrapper elements that
# merely CONTAIN the text -- the smallest match is the actual control. This
# is the same trap that mis-targeted the Banker spot during baccarat work.
_FIND_CONTROLS_JS = """(needles) => {
    const out = [];
    const els = Array.from(document.querySelectorAll('div, span, button, a'));
    for (const e of els) {
        const raw = (e.innerText || '').replace(/[\\u2066\\u2069\\u200b]/g, '').trim();
        const t = raw.toUpperCase();
        if (!t || t.length > 40) continue;
        for (const n of needles) {
            if (!t.includes(n)) continue;
            const r = e.getBoundingClientRect();
            out.push({
                needle: n, tag: e.tagName, text: raw.slice(0, 40),
                role: e.getAttribute('data-role') || '',
                cls: (e.className || '').toString().slice(0, 60),
                w: Math.round(r.width), h: Math.round(r.height),
                x: Math.round(r.left), y: Math.round(r.top),
                visible: r.width > 0 && r.height > 0,
                disabled: !!e.disabled || e.getAttribute('aria-disabled') === 'true',
                pointer: getComputedStyle(e).pointerEvents,
                opacity: getComputedStyle(e).opacity,
            });
            break;
        }
    }
    // smallest first -- the real control, not its wrapper
    out.sort((a, b) => (a.w * a.h) - (b.w * b.h));
    return out.slice(0, 40);
}"""

CONTROL_NEEDLES = ["UP", "DOWN", "CASH OUT", "PORTFOLIO", "TOTAL BET",
                   "BALANCE", "PLACE YOUR BETS", "MAKE YOUR DECISION",
                   "NEXT GAME", "REPEAT", "DOUBLE", "UNDO", "1% FEE"]


def show(label, rows):
    print(f"\n=== {label} ({len(rows)}) ===")
    for r in rows:
        print("   ", json.dumps(r, ensure_ascii=False))


def dump_frame(frame, label):
    print(f"\n{'=' * 68}\n{label}\n{'=' * 68}")
    try:
        roles = frame.evaluate(_DUMP_ROLES_JS)
    except Exception as e:
        print(f"  could not read roles: {str(e)[:120]}")
        return
    vis = [r for r in roles if r["visible"]]
    hid = [r for r in roles if not r["visible"]]
    show("VISIBLE data-role elements", vis)
    print(f"\n  ({len(hid)} hidden data-role elements omitted; "
          f"roles: {sorted({r['role'] for r in hid})})")
    try:
        show("CONTROL CANDIDATES by text (smallest first)",
             frame.evaluate(_FIND_CONTROLS_JS, CONTROL_NEEDLES))
    except Exception as e:
        print(f"  control scan failed: {str(e)[:120]}")

    # Do the existing production helpers work unchanged on this game?
    print("\n=== EXISTING HELPERS AGAINST THIS FRAME ===")
    print("    _read_total_bet()   ->", m._read_total_bet(frame))
    print("    read_game_balance() ->", m.read_game_balance(frame))
    print("    _betting_open()     ->", m._betting_open(frame),
          "  (baccarat's circle-timer probe)")


def watch(frame, game_page, seconds):
    """Sample the frame across a full round so we can see which roles appear
    and disappear per phase -- that transition IS the betting-window and
    cash-out-window detector we need."""
    print(f"\n{'=' * 68}\nWATCHING {seconds}s -- role-set changes per phase\n{'=' * 68}")
    prev = None
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            names = frame.evaluate(_ROLE_NAMES_JS)
            tb = m._read_total_bet(frame)
            bal = m.read_game_balance(frame)
            timer = m._betting_open(frame)
        except Exception as e:
            print(f"  [{time.strftime('%H:%M:%S')}] sample failed: {str(e)[:80]}")
            time.sleep(2)
            continue

        stamp = time.strftime("%H:%M:%S")
        if names != prev:
            added = sorted(set(names) - set(prev or []))
            gone = sorted(set(prev or []) - set(names))
            print(f"\n  [{stamp}] ROLES CHANGED  circle-timer={timer}  "
                  f"total_bet={tb}  balance={bal}")
            if added:
                print(f"      + {added}")
            if gone:
                print(f"      - {gone}")
            prev = names
        else:
            print(f"  [{stamp}] steady  circle-timer={timer}  "
                  f"total_bet={tb}  balance={bal}")
        time.sleep(2)


with sync_playwright() as p:
    proxy_conf = m.parse_proxy(args.proxy) if args.proxy else None
    bridge_proc = None
    if proxy_conf:
        proxy_conf, bridge_proc = m.maybe_bridge_proxy(proxy_conf)

    browser = p.chromium.launch(headless=False, slow_mo=100)
    context = browser.new_context(proxy=proxy_conf) if proxy_conf else browser.new_context()
    page = context.new_page()

    try:
        print(f"--- Logging in as {args.username!r} at {SITE_URL} ---")
        outcome, msgs = m.login(page, args.username, args.password, site_url=SITE_URL)
        print(f"    login outcome={outcome!r} messages={msgs}")
        if outcome != "ok":
            print("    Login did not succeed. Fix that before inspecting the game.")
            input("    Press Enter to close ...")
            sys.exit(1)

        print("""
--- Now drive the browser yourself ---
Open Stock Market Live however you like, and note WHICH path worked:
  (a) the site's own search box   (b) a Live Casino category tab
  (c) opening any Evolution game, then searching in the provider's lobby
If a "CHOOSE CHIPS" gate appears, pick REAL CHIPS.
""")
        input("Press Enter once the Stock Market table is loaded and running ...")

        # The game may be in this tab (same-tab vt_id launch) or a new one.
        game_page, frame = None, None
        for pg in context.pages:
            if "evo-games" in pg.url or "ezugi" in pg.url:
                fr = m.find_game_frame(pg, "evo-games.com", timeout_ms=8000)
                if fr:
                    game_page, frame = pg, fr
                    break
        if frame is None:
            print("\nCould not find an Evolution game frame. Tabs currently open:")
            for pg in context.pages:
                print("   ", pg.url[:120])
            input("Press Enter to close ...")
            sys.exit(1)

        print(f"\n    game tab url : {game_page.url[:160]}")
        print(f"    table id     : {m._table_id(game_page)}")
        print(f"    frame url    : {frame.url[:160]}")

        dump_frame(frame, "INITIAL DUMP")
        try:
            game_page.screenshot(path="shots/inspect-stock-initial.png")
        except Exception:
            pass

        watch(frame, game_page, args.watch)

        dump_frame(frame, "FINAL DUMP (after watching a full round)")
        try:
            game_page.screenshot(path="shots/inspect-stock-final.png")
        except Exception:
            pass

        print("""
--- One more thing to check by hand ---
Stake the table minimum on ONE side and let the round run WITHOUT pressing
CASH OUT. Does it auto-settle, or is the stake forfeited? We need to know how
a failed cash-out degrades before automating it.
""")
        input("Press Enter to dump once more (e.g. while a bet is live), or just Enter to finish ...")
        dump_frame(frame, "AD-HOC DUMP")
    finally:
        try:
            context.close()
        except Exception:
            pass
        try:
            browser.close()
        except Exception:
            pass
        m.stop_bridge(bridge_proc)
        print("\nDone. Review the dump above + shots/inspect-stock-*.png before "
              "any production code is written.")
