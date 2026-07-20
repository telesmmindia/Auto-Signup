"""One-off inspector for Evolution's "Stock Market Live" game.

Discovery only -- this places NO bets and clicks nothing inside the game
frame. It only navigates the lobby and opens a game tile, exactly like the
already-verified baccarat path does. Same precedent as inspect_casino.py:
run it, read the printed dump, and only then write production selectors.
Baccarat's data-role convention (bet-spot-Banker / circle-timer /
balance-label-value) may or may not carry over to this game, and guessing
with real money is not acceptable.

What it answers (the Phase 0 unknowns):
  * how to reach Stock Market Live at all -- it dumps every lobby category
    tab, search box and stock-ish tile, then tries several nav paths and
    reports which one worked
  * every [data-role] present in the game frame, and how that set changes
    across a full round -- this is how we learn the betting-window and
    cash-out-window detectors
  * where UP / DOWN / CASH OUT / PORTFOLIO actually live in the DOM
  * whether the existing _read_total_bet / read_game_balance / _betting_open
    helpers carry over unchanged

Usage:
  .venv/bin/python inspect_stockmarket.py <username> <password>
      [--url URL] [--proxy P] [--watch SECONDS] [--manual] [--headed]
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
parser.add_argument("--watch", type=int, default=150,
                    help="seconds to watch the live table (default 150, ~2-3 rounds)")
parser.add_argument("--manual", action="store_true",
                    help="don't auto-navigate; pause so you can open the game by hand")
parser.add_argument("--headed", action="store_true", help="show the browser")
parser.add_argument("--nav-only", action="store_true",
                    help="stop once the game opens; skip the round watch")
parser.add_argument("--no-path-d", action="store_true",
                    help="skip the slow open-Baccarat-then-search fallback")
args = parser.parse_args()

SITE_URL = args.url or m.SITE_URL

# Tile text / category guesses, tried in order. Stock Market Live is an
# Evolution "game show" style title, so it may not sit under a Baccarat-like
# category tab at all -- hence the direct-tile and search attempts first.
TILE_GUESSES = ["Stock Market", "Stock Market Live", "STOCK MARKET"]
# Enumerated live from the lobby dump (2026-07-20). Two different tab strips
# exist and only one of them matters: the SIDEBAR lists Blackjack / Roulette /
# Baccarat / Andar Bahar / Teenpatti / Poker / TV Games, while the lobby's own
# filter strip lists Live Lobby / All / Promo / Roulette / Blackjack /
# Baccarat / Game Shows / Arcade Games. Evolution's game shows (Stock Market
# Live among them) belong under "Game Shows", so that's tried first.
# "EVOLUTION" is the provider-filter tile, not a category tab -- clicking it
# lists that provider's whole catalogue, which is where a game the lobby's own
# categories don't surface (Stock Market Live) would show up. Each category
# grid is truncated to ~14 tiles behind a "View All", so that gets clicked too.
CATEGORY_GUESSES = ["EVOLUTION", "Evolution", "Game Shows", "Arcade Games", "All"]


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

# Find controls by text, excluding the giant wrapper elements that merely
# CONTAIN the text -- the smallest match is the actual control. Same trap
# that mis-targeted the Banker spot during baccarat work.
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
    out.sort((a, b) => (a.w * a.h) - (b.w * b.h));  // smallest = the real control
    return out.slice(0, 40);
}"""

CONTROL_NEEDLES = ["UP", "DOWN", "CASH OUT", "PORTFOLIO", "TOTAL BET",
                   "BALANCE", "PLACE YOUR BETS", "MAKE YOUR DECISION",
                   "NEXT GAME", "REPEAT", "DOUBLE", "UNDO", "1% FEE"]

# Lobby reconnaissance: what can we actually click to reach the game?
_DUMP_LOBBY_JS = """() => {
    const vis = e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
    const tabs = Array.from(document.querySelectorAll('a, button'))
        .filter(vis)
        .map(e => (e.innerText || '').trim())
        .filter(t => t && t.length < 30);
    const inputs = Array.from(document.querySelectorAll('input')).filter(vis).map(e => ({
        id: e.id || '', name: e.name || '', type: e.type || '',
        placeholder: e.placeholder || '', cls: (e.className || '').toString().slice(0, 60),
    }));
    const stockish = Array.from(document.querySelectorAll('*'))
        .filter(e => vis(e) && /stock/i.test((e.innerText || '')) &&
                     (e.innerText || '').trim().length < 60)
        .map(e => {
            const r = e.getBoundingClientRect();
            return {tag: e.tagName, text: (e.innerText || '').trim().slice(0, 50),
                    cls: (e.className || '').toString().slice(0, 60),
                    w: Math.round(r.width), h: Math.round(r.height)};
        })
        .sort((a, b) => (a.w * a.h) - (b.w * b.h)).slice(0, 15);
    return {tabs: Array.from(new Set(tabs)).slice(0, 80), inputs, stockish};
}"""


# Game tiles carry their title as a short text node inside a smallish box.
# Dumping these per category is how we learn what's actually on offer and
# under which tab -- the lobby's tab list alone doesn't reveal game names.
_DUMP_TILES_JS = """() => {
    const vis = e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
    const seen = new Set();
    const out = [];
    for (const e of Array.from(document.querySelectorAll('div, span, a, p, h3, h4'))) {
        if (!vis(e)) continue;
        const t = (e.innerText || '').trim();
        if (!t || t.length > 32 || t.includes('\\n')) continue;
        const r = e.getBoundingClientRect();
        if (r.width > 320 || r.height > 90) continue;   // wrappers, not tiles
        if (seen.has(t)) continue;
        seen.add(t);
        out.push(t);
    }
    return out.slice(0, 500);
}"""


def scroll_all(page, rounds=12):
    """The lobby lazy-loads its game grid, so a single dump only ever sees the
    first ~100 tiles (confirmed live 2026-07-20: the EVOLUTION provider view
    cut off mid-alphabet at "Blackjack VIP"). Scroll to the bottom repeatedly
    until the height stops growing."""
    last = 0
    for _ in range(rounds):
        try:
            page.mouse.wheel(0, 20000)
            page.wait_for_timeout(900)
            h = page.evaluate("() => document.body.scrollHeight")
        except Exception:
            break
        if h == last:
            break
        last = h


def dump_tiles(page, label):
    try:
        tiles = page.evaluate(_DUMP_TILES_JS)
    except Exception as e:
        print(f"  tile dump failed: {str(e)[:100]}")
        return []
    print(f"\n=== TILE-ISH TEXTS: {label} ({len(tiles)}) ===")
    print("   ", json.dumps(tiles, ensure_ascii=False))
    hits = [t for t in tiles if "stock" in t.lower()]
    if hits:
        print(f"    *** STOCK MATCH: {hits} ***")
    return tiles


def show(label, rows):
    print(f"\n=== {label} ({len(rows)}) ===")
    for r in rows:
        print("   ", json.dumps(r, ensure_ascii=False))


def dump_lobby(page, label):
    print(f"\n{'=' * 68}\nLOBBY RECON: {label}\n  url={page.url[:140]}\n{'=' * 68}")
    try:
        info = page.evaluate(_DUMP_LOBBY_JS)
    except Exception as e:
        print(f"  lobby dump failed: {str(e)[:120]}")
        return {}
    print(f"\n=== CLICKABLE TABS/LINKS ({len(info['tabs'])}) ===")
    print("   ", json.dumps(info["tabs"], ensure_ascii=False))
    show("VISIBLE INPUTS (search boxes?)", info["inputs"])
    show("ANYTHING MENTIONING 'STOCK' (smallest first)", info["stockish"])
    return info


def dump_frame(frame, label):
    print(f"\n{'=' * 68}\n{label}\n{'=' * 68}")
    try:
        roles = frame.evaluate(_DUMP_ROLES_JS)
    except Exception as e:
        print(f"  could not read roles: {str(e)[:120]}")
        return
    vis = [r for r in roles if r["visible"]]
    hid = [r for r in roles if not r["visible"]]
    # The baccarat frame carries ~700 identical roadItem/roadItemColor SVGs
    # (the scoreboard grid). Keep a couple of each repeated role so the real
    # controls aren't buried.
    counts, kept = {}, []
    for r in vis:
        counts[r["role"]] = counts.get(r["role"], 0) + 1
        if counts[r["role"]] <= 2:
            kept.append(r)
    show("VISIBLE data-role elements (max 2 per role)", kept)
    noisy = {k: v for k, v in counts.items() if v > 2}
    if noisy:
        print(f"\n  (collapsed repeats: {json.dumps(noisy)})")
    print(f"\n  ({len(hid)} hidden data-role elements omitted; "
          f"roles: {sorted({r['role'] for r in hid})})")
    try:
        show("CONTROL CANDIDATES by text (smallest first)",
             frame.evaluate(_FIND_CONTROLS_JS, CONTROL_NEEDLES))
    except Exception as e:
        print(f"  control scan failed: {str(e)[:120]}")

    print("\n=== EXISTING HELPERS AGAINST THIS FRAME ===")
    print("    _read_total_bet()   ->", m._read_total_bet(frame))
    print("    read_game_balance() ->", m.read_game_balance(frame))
    print("    _betting_open()     ->", m._betting_open(frame),
          "  (baccarat's circle-timer probe)")


def wait_for_game(context, page, before, timeout_ms=20000):
    """After a tile click, the provider opens either a NEW tab (table_id=) or
    redirects THIS tab (vt_id=, the bonus/REAL-CHIPS path). Handles both, and
    keeps re-dismissing the CHOOSE CHIPS gate meanwhile -- same dual handling
    as search_and_open_game()."""
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        for pg in context.pages:
            if pg not in before and ("evo-games" in pg.url or "ezugi" in pg.url):
                return pg
        if "evo-games" in page.url or "ezugi" in page.url:
            return page
        m._dismiss_choose_chips_modal(page)
        page.wait_for_timeout(500)
    return None


def reset_lobby(page):
    """Get back to the Live Casino lobby between paths. Confirmed live
    (2026-07-20): the site search navigates to a full search-results view, so
    without this the next path finds neither the filter tabs nor the game
    tiles and fails for the wrong reason."""
    try:
        m.open_casino_lobby(page, timeout_ms=15000)
        m._dismiss_casino_promo_modal(page)
    except Exception as e:
        print(f"    reset_lobby: {str(e)[:80]}")


def click_tab(page, label):
    """Click a lobby filter tab. Confirmed live (2026-07-20): these are NOT
    <a> or <button> elements -- "Game Shows" and "Arcade Games" appear only
    in a div/span scan, which is why an a:has-text() locator silently never
    matched them. Match on exact text across any tag instead."""
    for name, loc in (("get_by_text", page.get_by_text(label, exact=True)),
                      ("a:has-text", page.locator(f"a:has-text('{label}')"))):
        try:
            n = loc.count()
            if not n:
                continue
            for i in range(min(n, 4)):   # first match may be an offscreen dupe
                item = loc.nth(i)
                if item.is_visible():
                    item.click(timeout=5000, force=True)
                    return True
            print(f"      {label!r}: {n} {name} match(es), none visible")
        except Exception as e:
            print(f"      {label!r} via {name}: {str(e)[:70]}")
    return False


def try_open_stock_market(context, page):
    """Try each known nav path and report which one worked. Lobby clicks only
    -- nothing here can place a bet."""
    m._dismiss_casino_promo_modal(page)

    # Path A: the tile is already on screen (no category filtering needed).
    for tile in TILE_GUESSES:
        try:
            loc = page.locator(f"text={tile}")
            if loc.count() and loc.first.is_visible():
                print(f"\n>>> PATH A: clicking visible tile {tile!r}")
                before = list(context.pages)
                loc.first.click(timeout=5000, force=True)
                gp = wait_for_game(context, page, before)
                if gp:
                    print(f">>> PATH A WORKED via tile {tile!r}")
                    return gp
        except Exception as e:
            print(f"    path A {tile!r}: {str(e)[:80]}")

    # Path C: category tabs. "TV Games" is the likely home for Evolution game
    # shows; each category's tiles are dumped so we learn what's there even
    # when Stock Market isn't.
    for cat in CATEGORY_GUESSES:
        try:
            print(f"\n>>> PATH C: category tab {cat!r}")
            if not click_tab(page, cat):
                print(f"    path C {cat!r}: tab not found/visible")
                continue
            page.wait_for_timeout(3000)
            scroll_all(page)
            dump_tiles(page, f"category {cat!r}")
            # Each category shows a truncated grid with a "View All" link --
            # the full catalogue (and Stock Market, if it's carried at all)
            # may only appear after expanding it.
            if click_tab(page, "View All"):
                page.wait_for_timeout(3000)
                dump_tiles(page, f"category {cat!r} + View All")
            for tile in TILE_GUESSES:
                loc = page.locator(f"text={tile}")
                if loc.count() and loc.first.is_visible():
                    before = list(context.pages)
                    loc.first.click(timeout=5000, force=True)
                    gp = wait_for_game(context, page, before)
                    if gp:
                        print(f">>> PATH C WORKED via category {cat!r} + tile {tile!r}")
                        return gp
        except Exception as e:
            print(f"    path C {cat!r}: {str(e)[:80]}")

    # Path D: the route the demo video actually used -- open any Evolution
    # game (Baccarat A is already proven to launch), then use the PROVIDER's
    # own in-game lobby search for "Stock". Dumps the game frame's controls
    # either way, so a failure still teaches us the search selector.
    reset_lobby(page)

    # Path B: the site's own search box. A plain fill() produced no results
    # live (2026-07-20) -- this SPA's search only reacts to real key events,
    # so type character-by-character and press Enter.
    for sel in ["input[placeholder*='Search' i]", "input[type='search']"]:
        try:
            box = page.locator(sel)
            if not (box.count() and box.first.is_visible()):
                continue
            print(f"\n>>> PATH B: typing 'Stock' into {sel!r} (real key events)")
            box.first.click(timeout=3000)
            box.first.type("Stock", delay=150)
            page.wait_for_timeout(2500)
            dump_tiles(page, "after typing 'Stock'")
            box.first.press("Enter")
            page.wait_for_timeout(3000)
            dump_tiles(page, "after pressing Enter")
            for tile in TILE_GUESSES:
                loc = page.locator(f"text={tile}")
                if loc.count() and loc.first.is_visible():
                    before = list(context.pages)
                    loc.first.click(timeout=5000, force=True)
                    gp = wait_for_game(context, page, before)
                    if gp:
                        print(f">>> PATH B WORKED via search + tile {tile!r}")
                        return gp
        except Exception as e:
            print(f"    path B {sel!r}: {str(e)[:80]}")

    if args.no_path_d:
        print("\n>>> PATH D skipped (--no-path-d)")
        return None
    reset_lobby(page)
    print("\n>>> PATH D: open Baccarat A, then search inside Evolution's own lobby")
    try:
        before = list(context.pages)
        if not m.search_and_open_game(page, "Baccarat", "Baccarat A"):
            print("    path D: could not open Baccarat A")
            return None
        gp = wait_for_game(context, page, before, timeout_ms=25000)
        if gp is None:
            print("    path D: Baccarat tile clicked but no game tab appeared")
            return None
        fr = m.find_game_frame(gp, "evo-games.com", timeout_ms=25000)
        if fr is None:
            print("    path D: no Evolution frame in the game tab")
            return None
        print(f"    path D: in Evolution frame {fr.url[:100]}")
        dump_frame(fr, "PATH D: BACCARAT FRAME (looking for the lobby/search control)")

        # The provider's lobby search is a magnifier icon in the top bar.
        for sel in ["[data-role*='search' i]", "[class*='search' i]",
                    "[aria-label*='search' i]", "input"]:
            try:
                loc = fr.locator(sel)
                if not (loc.count() and loc.first.is_visible()):
                    continue
                print(f"    path D: trying search control {sel!r} ({loc.count()} matches)")
                loc.first.click(timeout=3000, force=True)
                gp.wait_for_timeout(1500)
                box = fr.locator("input").first
                box.click(timeout=3000)
                box.type("Stock", delay=150)
                gp.wait_for_timeout(3000)
                for tile in TILE_GUESSES:
                    t = fr.locator(f"text={tile}")
                    if t.count() and t.first.is_visible():
                        print(f">>> PATH D FOUND tile {tile!r} in the Evolution lobby")
                        t.first.click(timeout=5000, force=True)
                        gp.wait_for_timeout(6000)
                        print(f">>> PATH D WORKED -- now at {gp.url[:120]}")
                        return gp
            except Exception as e:
                print(f"    path D {sel!r}: {str(e)[:80]}")
    except Exception as e:
        print(f"    path D failed: {str(e)[:120]}")
    return None


def watch(frame, game_page, seconds):
    """Sample across a full round so we can see which roles appear and
    disappear per phase -- that transition IS the betting-window and
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

    browser = p.chromium.launch(headless=not (args.headed or args.manual), slow_mo=100)
    context = browser.new_context(proxy=proxy_conf) if proxy_conf else browser.new_context()
    page = context.new_page()

    try:
        print(f"--- Logging in as {args.username!r} at {SITE_URL} ---")
        outcome, msgs = m.login(page, args.username, args.password, site_url=SITE_URL)
        print(f"    login outcome={outcome!r} messages={msgs}")
        if outcome != "ok":
            print("    Login did not succeed. Fix that before inspecting the game.")
            sys.exit(1)

        print("--- Opening the Live Casino lobby ---")
        print(f"    open_casino_lobby -> {m.open_casino_lobby(page, timeout_ms=20000)}")
        dump_lobby(page, "live casino lobby")
        try:
            page.screenshot(path="shots/inspect-stock-lobby.png")
        except Exception:
            pass

        if args.manual:
            input("\nOpen Stock Market Live by hand, then press Enter ...")
            game_page = next((pg for pg in context.pages
                              if "evo-games" in pg.url or "ezugi" in pg.url), None)
        else:
            game_page = try_open_stock_market(context, page)

        if game_page is None:
            print("\n!!! Could not reach Stock Market Live automatically.")
            print("    Read the LOBBY RECON dump above for the real tab/tile names,")
            print("    then re-run with --manual --headed to drive it by hand.")
            dump_lobby(page, "final state after failed nav")
            sys.exit(2)

        print(f"\n    game tab url : {game_page.url[:160]}")
        print(f"    table id     : {m._table_id(game_page)}")

        frame = m.find_game_frame(game_page, "evo-games.com", timeout_ms=20000)
        if frame is None:
            print("    Could not find the Evolution game frame. Frames present:")
            for fr in game_page.frames:
                print("       ", fr.url[:120])
            sys.exit(3)
        print(f"    frame url    : {frame.url[:160]}")
        print(f"    wait_for_live_table -> {m.wait_for_live_table(frame, game_page)}"
              "   (expected False: it probes for bet-spot-Banker)")

        dump_frame(frame, "INITIAL DUMP")
        try:
            game_page.screenshot(path="shots/inspect-stock-initial.png")
        except Exception:
            pass

        if args.nav_only:
            print("\n--nav-only: stopping before the round watch.")
            sys.exit(0)

        watch(frame, game_page, args.watch)

        dump_frame(frame, "FINAL DUMP (after watching a full round)")
        try:
            game_page.screenshot(path="shots/inspect-stock-final.png")
        except Exception:
            pass
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
