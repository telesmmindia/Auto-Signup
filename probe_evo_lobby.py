"""Probe the route to Evolution's "Stock Market Live", as described by the
site owner and shown in the demo video.

Established live 2026-07-20: Stock Market Live is NOT in cricmatch247's own
lobby catalogue -- 206 tiles across Game Shows / Arcade Games / All (with
"View All" expanded and lazy-load scrolled) contain no "Stock" match, and the
site search only returns football teams for "Stock". The game is only
reachable through the PROVIDER's lobby:

  1. search the site for "Super Sic Bo" and open it
  2. that lands in Evolution's Super Sic Bo Live
  3. a "Lobby" button sits at the BOTTOM RIGHT of the game frame -- click it
  4. search "Stock Market" in Evolution's own lobby
  5. open it

Read-only: this navigates and dumps DOM only. It places no bets.

Usage: .venv/bin/python probe_evo_lobby.py <username> <password> [--headed]
"""
import argparse
import json

from playwright.sync_api import sync_playwright

import main as m

parser = argparse.ArgumentParser()
parser.add_argument("username")
parser.add_argument("password")
parser.add_argument("--url", default=None)
parser.add_argument("--headed", action="store_true")
args = parser.parse_args()

SITE_URL = args.url or m.SITE_URL

# Anything that could plausibly be the Lobby / menu / search control.
_UI_JS = """() => {
    const vis = e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
    const interesting = /lobby|menu|search|burger|nav|home|back|game-list|tab/i;
    const out = [];
    for (const e of Array.from(document.querySelectorAll('[data-role],button,svg,[class*=icon],div,span'))) {
        if (!vis(e)) continue;
        const role = e.getAttribute('data-role') || '';
        const cls = (e.className || '').toString();
        const aria = e.getAttribute('aria-label') || '';
        const txt = (e.innerText || '').trim();
        if (txt.length > 24) continue;
        if (!(interesting.test(role) || interesting.test(cls) ||
              interesting.test(aria) || /lobby/i.test(txt))) continue;
        const r = e.getBoundingClientRect();
        out.push({role, aria, txt: txt.slice(0, 24), tag: e.tagName, cls: cls.slice(0, 50),
                  w: Math.round(r.width), h: Math.round(r.height),
                  x: Math.round(r.left), y: Math.round(r.top)});
    }
    out.sort((a, b) => (a.w * a.h) - (b.w * b.h));
    return out.slice(0, 40);
}"""

_ALL_ROLES_JS = """() => {
    const vis = e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
    const seen = {};
    for (const e of Array.from(document.querySelectorAll('[data-role]'))) {
        if (!vis(e)) continue;
        const r = e.getAttribute('data-role');
        seen[r] = (seen[r] || 0) + 1;
    }
    return seen;
}"""

_TILES_JS = """() => {
    const vis = e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
    const seen = new Set();
    for (const e of Array.from(document.querySelectorAll('div,span,a,p'))) {
        if (!vis(e)) continue;
        const t = (e.innerText || '').trim();
        if (t && t.length < 34 && !t.includes('\\n')) seen.add(t);
    }
    return Array.from(seen).slice(0, 400);
}"""


def site_search_open(context, page, query, tile):
    """Open a game via cricmatch's own search box. Confirmed live: the SPA
    only reacts to real key events, so fill() is not enough."""
    # The "Victory Boost" promo modal can pop up AFTER the lobby has settled
    # and swallows the search click (confirmed live: bonuspage_Popup-container-bg
    # "intercepts pointer events"), so dismiss it immediately before typing.
    m._dismiss_casino_promo_modal(page)
    box = page.locator("input[placeholder*='Search' i]").first
    box.click(timeout=5000, force=True)
    box.type(query, delay=120)
    page.wait_for_timeout(3500)
    before = list(context.pages)
    # NOTE: do NOT click the "Casino"/"Slots" result chips by text -- those
    # strings also match the sidebar nav links, and clicking one navigates
    # away from the results entirely (confirmed live 2026-07-20).
    print("    result page texts:", json.dumps(page.evaluate(_TILES_JS)[-70:]))
    loc = page.locator(f"text={tile}")
    print(f"    search {query!r}: {loc.count()} match(es) for tile {tile!r}")
    if not loc.count():
        print("    visible result texts:", json.dumps(page.evaluate(_TILES_JS)[:60]))
        return None
    loc.first.click(timeout=5000, force=True)
    for i in range(80):
        for pg in context.pages:
            if pg not in before and ("evo-games" in pg.url or "ezugi" in pg.url):
                return pg
        if "evo-games" in page.url or "ezugi" in page.url:
            return page
        m._dismiss_choose_chips_modal(page)
        m._dismiss_casino_promo_modal(page)
        if i and i % 20 == 0:
            print(f"      still waiting ({i//2}s); this tab: {page.url[:90]}; "
                  f"{len(context.pages)} tab(s)")
            try:
                page.screenshot(path=f"shots/probe-waiting-{i}.png")
            except Exception:
                pass
        page.wait_for_timeout(500)
    return None


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

        print("--- lobby ---", m.open_casino_lobby(page, timeout_ms=20000))
        m._dismiss_casino_promo_modal(page)

        # Step 1: get into ANY Evolution game -- the Lobby button we're after
        # is the provider's own chrome, not specific to Super Sic Bo. The
        # site search route the video uses is unreliable to automate (its
        # overlay closes on the tile click without launching, confirmed live
        # 2026-07-20), whereas search_and_open_game("Baccarat","Baccarat A")
        # is already verified in production, so use that as the door in.
        print("--- step 1: open Baccarat A (proven Evolution entry point) ---")
        # The promo modal silently swallows tile clicks (documented in
        # CLAUDE.md: "even a force=True click on the actual game tile silently
        # no-ops while this modal is open"), and it can reappear at any time --
        # so retry the way _open_table_for does rather than trusting one shot.
        gp = None
        for attempt in range(1, 4):
            m._dismiss_casino_promo_modal(page)
            before = list(context.pages)
            res = m.search_and_open_game(page, "Baccarat", "Baccarat A")
            print(f"    attempt {attempt}: search_and_open_game -> {res}")
            gp = next((x for x in context.pages
                       if x not in before and ("evo-games" in x.url or "ezugi" in x.url)), None)
            if gp is None and ("evo-games" in page.url or "ezugi" in page.url):
                gp = page
            if gp is not None:
                break
            page.wait_for_timeout(2500)
            m.open_casino_lobby(page, timeout_ms=15000)
        if gp is None:
            print("!!! could not open Super Sic Bo. Open tabs:")
            for x in context.pages:
                print("   ", x.url[:120])
            raise SystemExit(2)
        print("    game url:", gp.url[:140])

        fr = m.find_game_frame(gp, "evo-games.com", timeout_ms=30000)
        if fr is None:
            raise SystemExit(3)
        print("    frame:", fr.url[:140])
        gp.wait_for_timeout(9000)   # let the table finish loading

        print("\n=== step 2: ROLES IN SUPER SIC BO ===")
        print(json.dumps(dict(sorted(fr.evaluate(_ALL_ROLES_JS).items())), indent=1))

        print("\n=== LOBBY BUTTON CANDIDATES (bottom-right expected) ===")
        for c in fr.evaluate(_UI_JS):
            print("   ", json.dumps(c, ensure_ascii=False))
        gp.screenshot(path="shots/probe-sicbo.png")

        print("\n--- step 3: click the Lobby button ---")
        clicked = False
        for sel in ["text=Lobby", "[data-role*='lobby' i]", "[aria-label*='lobby' i]",
                    "[class*='lobby' i]"]:
            try:
                loc = fr.locator(sel)
                if loc.count() and loc.first.is_visible():
                    loc.first.click(timeout=4000, force=True)
                    print(f"    clicked {sel!r}")
                    clicked = True
                    break
            except Exception as e:
                print(f"    {sel!r}: {str(e)[:70]}")
        if not clicked:
            print("!!! no Lobby control found -- see candidates above")
            raise SystemExit(4)
        gp.wait_for_timeout(6000)
        gp.screenshot(path="shots/probe-evo-lobby.png")

        print("\n=== step 4: EVOLUTION LOBBY CONTENTS ===")
        tiles = fr.evaluate(_TILES_JS)
        print(f"    {len(tiles)} texts; stock hits: "
              f"{[t for t in tiles if 'stock' in t.lower()]}")
        print("\n=== LOBBY SEARCH CANDIDATES ===")
        for c in fr.evaluate(_UI_JS):
            print("   ", json.dumps(c, ensure_ascii=False))
        inputs = fr.locator("input")
        print(f"    inputs in frame: {inputs.count()}")

        # The lobby has a real Search box top-right (placeholder "Search") and
        # a "Game Shows" category tab -- both are viable routes to the game.
        # The lobby overlay is fragile -- any stray click (a coordinate
        # click, a click on the wrong input) dismisses it and drops back into
        # the game, which is what happened live 2026-07-20. So go straight for
        # the lobby's own "Game Shows" tab with no exploratory clicking, and
        # dump whatever it renders.
        # The lobby's search box is NOT an <input> in the game frame (the only
        # one there is quick-chat-input), so the in-game lobby very likely
        # renders in a SEPARATE iframe -- find_game_frame() returns the frame
        # with the most divs, which is the game itself. Enumerate every frame
        # while the lobby is open and dump whichever one holds it.
        # THE KEY FINDING (live 2026-07-20): the in-game lobby is a SEPARATE
        # iframe -- url contains "?iFrAmE=x" and "category=" -- with its own
        # real Search input. find_game_frame() returns the GAME frame, which
        # has no search box at all (only quick-chat-input), which is why every
        # earlier attempt to type into "the frame" went nowhere.
        print("\n--- step 5: locate the lobby frame and search it ---")
        lobby_fr = None
        for f in gp.frames:
            try:
                txt = f.evaluate(_TILES_JS)
                if any(t in ("For You", "Top Games", "Game Shows") for t in txt):
                    lobby_fr = f
                    break
            except Exception:
                continue
        if lobby_fr is None:
            print("!!! lobby frame not found")
        else:
            print(f"    lobby frame: {lobby_fr.url[:110]}")
            try:
                box = lobby_fr.get_by_placeholder("Search").first
                box.click(timeout=5000)
                box.type("Stock", delay=140)
                gp.wait_for_timeout(4500)
                print("    typed 'Stock' into the lobby search")
            except Exception as e:
                print(f"    search typing failed: {str(e)[:100]}")
            gp.screenshot(path="shots/probe-evo-search.png")
            txt = lobby_fr.evaluate(_TILES_JS)
            hits = [t for t in txt if "stock" in t.lower()]
            print(f"    *** STOCK HITS: {hits} ***")
            print(f"    results: {json.dumps(txt[:60], ensure_ascii=False)}")

            if hits:
                print("\n--- step 6: open Stock Market and dump it ---")
                try:
                    lobby_fr.locator("text=Stock Market").first.click(
                        timeout=6000, force=True)
                except Exception as e:
                    print(f"    tile click: {str(e)[:90]}")
                gp.wait_for_timeout(12000)
                gp.screenshot(path="shots/probe-stockmarket.png")
                print("    url now:", gp.url[:140])

                # The game frame changes when a new table loads, so re-resolve
                # it rather than reusing the Baccarat one.
                sm = None
                for f in gp.frames:
                    try:
                        t = f.evaluate(_TILES_JS)
                        if any(x.upper() in ("UP", "DOWN", "CASH OUT", "PORTFOLIO")
                               for x in t):
                            sm = f
                            break
                    except Exception:
                        continue
                if sm is None:
                    sm = m.find_game_frame(gp, "evo-games.com", timeout_ms=20000)
                if sm is None:
                    print("!!! no Stock Market frame found")
                else:
                    print("    frame:", sm.url[:120])
                    print("\n=== STOCK MARKET ROLES ===")
                    print(json.dumps(dict(sorted(sm.evaluate(_ALL_ROLES_JS).items())), indent=1))
                    print("\n=== TEXTS ===")
                    print(json.dumps(sm.evaluate(_TILES_JS)[:120], ensure_ascii=False))
                    print("\n=== EXISTING HELPERS ===")
                    print("    _read_total_bet   ->", m._read_total_bet(sm))
                    print("    read_game_balance ->", m.read_game_balance(sm))
                    print("    _betting_open     ->", m._betting_open(sm))
                    print("\n=== WATCHING 90s (phase transitions) ===")
                    import time as _t
                    prev = None
                    end_at = _t.time() + 90
                    while _t.time() < end_at:
                        try:
                            names = sorted(sm.evaluate(_ALL_ROLES_JS).keys())
                            tb = m._read_total_bet(sm)
                            bal = m.read_game_balance(sm)
                        except Exception as e:
                            print("    sample failed:", str(e)[:70]); _t.sleep(3); continue
                        if names != prev:
                            print(f"  [{_t.strftime('%H:%M:%S')}] +{sorted(set(names)-set(prev or []))} "
                                  f"-{sorted(set(prev or [])-set(names))} tb={tb} bal={bal}")
                            prev = names
                        _t.sleep(3)
                    gp.screenshot(path="shots/probe-stockmarket-end.png")

        print("\nscreenshots -> shots/probe-sicbo.png, probe-evo-lobby.png, "
              "probe-evo-search.png")
    finally:
        try:
            context.close()
        except Exception:
            pass
        browser.close()
