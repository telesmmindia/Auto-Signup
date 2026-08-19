"""Read-only discovery probe for starexch555's live casino. NEVER places a bet.

Why this exists: starexch's baccarat is NOT the Evolution game every other site
in this repo drives. It is Ezugi's own client, and none of the tournament /
hedge engine's selectors match it. This script drives the verified route to a
live table and dumps the hooks a future Ezugi driver needs, so nobody has to
re-derive any of it.

Verified live 2026-08-19 (account DasVarma4222):

  Route to a table -- all of it different from cricmatch:
    1. login() works with the cricmatch selectors (sites/starexch.py).
    2. Clicking a "Live Casino" nav element does NOT work. Every candidate
       (div.nb_rdlink[data-href], a[href*=live-casino], text=Live Casino)
       either times out or no-ops.
    3. A direct page.goto("/live-casino/?p=<provider>") DOES work AND KEEPS
       THE SESSION (wallet still readable afterwards). This is the opposite of
       cricmatch, where open_casino_lobby()'s comment records that a hard load
       drops the logged-in view -- do not "fix" this back to a click.
    4. Tiles are opened by the site's own handler:
       div[onclick="goToCasinoLive(this)"][data-id=...][data-provider=...],
       which sits on the tile's IMAGE container. The <p class="game__name">
       label is NOT clickable -- clicking it silently does nothing.
    5. The provider matters. "?p=All" surfaces a jacktop "Baccarat" tile;
       the Ezugi tables (Baccarat A..E, A = data-id 1014) are under
       "?p=ezugi". Only Ezugi/Evolution tables are worth driving.
    6. The table opens in a NEW TAB on a RANDOMISED white-label host
       (e.g. pxoki81qhmq.xoki81qhmq.com, different every launch), NOT
       ezugi.evo-games.com. So find_game_frame()'s host_hint cannot be
       hardcoded -- and the game is not in an iframe at all here, it IS the
       tab's own document.

  The table UI (Ezugi, NOT Evolution -- see CLAUDE.md):
    balance      [data-e2e="balance-value"]            "₹ 180.00"
    total bet    [data-e2e="total-bet-value"]          "₹ 0"
    limits       [data-e2e="footer-table-info-value"]  "₹ 100 - ₹ 1,000,000"
    timer        [data-e2e="betting-timer"] / [data-testid="time-left"]
    chips        [data-e2e="chip-<value>"] with data-value / data-selected /
                 data-disabled -- data-disabled flips to "true" between
                 rounds, which is a cleaner betting-window signal than
                 Evolution's circle-timer presence check.
    bet spots    the Banker/Player spots carry NO data-e2e (only the Pair
                 side bets do). They are labelled by
                 span.bet-label with the exact text "Banker" / "Player"
                 ("Tie" is a plain span, no bet-label class), each ~103x18
                 at a distinct x -- Player left of Banker. The clickable spot
                 is the label's container, so this still needs the same
                 smallest-enclosing-element targeting _TAG_BET_SPOT_JS does
                 for Evolution. Confirmed present on a live table 2026-08-19.

Usage:
  .venv/bin/python probe_starexch_casino.py <user> <pass> [--headed]
        [--provider ezugi] [--tile "Baccarat A"] [--open] [--secs 20]
"""
import sys
import time

import main as engine
from playwright.sync_api import sync_playwright

argv = sys.argv[1:]


def flag(name, default=None):
    if name in argv:
        i = argv.index(name)
        val = argv[i + 1] if i + 1 < len(argv) else default
        del argv[i:i + 2]
        return val
    return default


HEADED = "--headed" in argv
OPEN_TABLE = "--open" in argv
PROVIDER = flag("--provider", "ezugi")
TILE = flag("--tile", "Baccarat A")
SECS = int(flag("--secs", "20"))
argv = [a for a in argv if not a.startswith("--")]
if len(argv) < 2:
    print(__doc__)
    sys.exit(1)
USERNAME, PASSWORD = argv[0], argv[1]
SITE = "https://starexch555.com"

# One JS pass that inventories every tagged, visible element -- the dump this
# script exists to produce.
_INVENTORY_JS = """() => {
  const out = [];
  document.querySelectorAll(
    '[data-e2e],[data-testid],[data-test-id],[data-role],[data-value]'
  ).forEach(e => {
    const r = e.getBoundingClientRect();
    if (r.width < 3 || r.height < 3) return;
    out.push({
      e2e: e.getAttribute('data-e2e') || '',
      testid: e.getAttribute('data-testid') || e.getAttribute('data-test-id') || '',
      role: e.getAttribute('data-role') || '',
      value: e.getAttribute('data-value') || '',
      selected: e.getAttribute('data-selected') || '',
      disabled: e.getAttribute('data-disabled') || '',
      tag: e.tagName,
      text: (e.innerText || '').trim().slice(0, 30)
                .split(String.fromCharCode(10)).join('|'),
      size: Math.round(r.width) + 'x' + Math.round(r.height)
    });
  });
  return out;
}"""


def open_lobby(page, provider):
    """Direct goto -- see the module docstring for why this is NOT a click."""
    for attempt in range(3):
        try:
            page.wait_for_timeout(2500)
            page.goto(f"{SITE}/live-casino/?p={provider}",
                      wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(5000)
            engine.dismiss_popups(page)
            return True
        except Exception as e:
            print(f"  goto attempt {attempt + 1}: {type(e).__name__}")
    return False


def list_tiles(page):
    return page.evaluate("""() => {
      const out = [], seen = new Set();
      document.querySelectorAll('[onclick*="goToCasinoLive"]').forEach(e => {
        const par = e.closest('.partclrGamesParDv,.partclrGamesParDvMob');
        const nm = par && par.querySelector('.game__name');
        const name = nm ? nm.innerText.trim() : '';
        const id = e.getAttribute('data-id') || '';
        const key = name + '|' + id;
        if (!name || seen.has(key)) return;
        seen.add(key);
        out.push({name: name, id: id,
                  provider: e.getAttribute('data-provider') || '',
                  clickable: e.getBoundingClientRect().width > 5});
      });
      return out;
    }""")


def click_tile(page, tile_text):
    """Click the tile's own handler element, not its name label."""
    return page.evaluate("""(want) => {
      const els = [...document.querySelectorAll('[onclick*="goToCasinoLive"]')];
      const t = els.find(e => {
        const par = e.closest('.partclrGamesParDv,.partclrGamesParDvMob');
        const nm = par && par.querySelector('.game__name');
        return nm && nm.innerText.trim() === want
               && e.getBoundingClientRect().width > 5;
      });
      if (!t) return '';
      t.click();
      return t.getAttribute('data-id') || '?';
    }""", tile_text)


with sync_playwright() as p:
    browser = p.chromium.launch(headless=not HEADED)
    context = browser.new_context()
    page = context.new_page()
    try:
        outcome, msgs = engine.login(page, USERNAME, PASSWORD, site_url=SITE)
        print("login:", outcome, msgs)
        if outcome != "ok":
            sys.exit(1)

        if not open_lobby(page, PROVIDER):
            print("could not reach the lobby")
            sys.exit(1)
        print("lobby url:", page.url)
        # Prove the session survived the hard load (cricmatch's does not).
        print("wallet after direct goto:", engine.read_wallet_balance(page, timeout_secs=20))

        tiles = list_tiles(page)
        print(f"\n--- tiles under ?p={PROVIDER} ({len(tiles)}) ---")
        for t in tiles:
            mark = "  " if t["clickable"] else " (hidden)"
            print(f"   {t['name'][:38]:40} id={t['id']:>6} provider={t['provider']}{mark}")

        if not OPEN_TABLE:
            print("\n(pass --open to also open a table and dump its hooks)")
            sys.exit(0)

        before = len(context.pages)
        got = click_tile(page, TILE)
        print(f"\nclicked tile {TILE!r} -> data-id={got or 'NOT FOUND'}")
        for _ in range(12):
            page.wait_for_timeout(2500)
            if len(context.pages) > before:
                break
        try:
            engine._dismiss_choose_chips_modal(page)
        except Exception:
            pass

        target = context.pages[-1] if len(context.pages) > before else page
        print("opened in a new tab:", len(context.pages) > before)
        print("table url:", target.url[:120])
        print("table_id:", engine._table_id(target))
        print(f"waiting {SECS}s for the table to render...")
        target.wait_for_timeout(SECS * 1000)

        inv = target.evaluate(_INVENTORY_JS)
        print(f"\n--- tagged visible elements ({len(inv)}) ---")
        seen = set()
        for h in inv:
            key = (h["e2e"], h["testid"], h["role"], h["value"])
            if key in seen:
                continue
            seen.add(key)
            bits = [f"{k}={v}" for k, v in h.items() if v and k != "tag"]
            print("   ", h["tag"], " ".join(bits))

        print("\n--- the specific hooks a driver needs ---")
        for label, sel in [
            ("balance", '[data-e2e="balance-value"]'),
            ("total bet", '[data-e2e="total-bet-value"]'),
            ("table limits", '[data-e2e="footer-table-info-value"]'),
            ("betting timer", '[data-e2e="betting-timer"]'),
        ]:
            try:
                el = target.locator(sel).first
                print(f"   {label:15} {sel:40} -> {el.inner_text().strip()!r}")
            except Exception as e:
                print(f"   {label:15} {sel:40} -> MISSING ({type(e).__name__})")

        chips = target.evaluate("""() => [...document.querySelectorAll('[data-e2e^="chip-"][data-value]')]
            .map(e => ({value: e.getAttribute('data-value'),
                        selected: e.getAttribute('data-selected'),
                        disabled: e.getAttribute('data-disabled')}))""")
        print("   chip rail:", chips)

        # The open question: what are the Banker/Player spots?
        print("\n--- candidates for the Banker / Player bet spots ---")
        spots = target.evaluate("""() => {
          const out = [];
          const want = /^(banker|player|tie)$/i;
          document.querySelectorAll('*').forEach(e => {
            const t = (e.innerText || '').trim();
            if (!want.test(t)) return;
            if (e.children.length > 3) return;
            const r = e.getBoundingClientRect();
            if (r.width < 5 || r.height < 5) return;
            const a = {};
            for (const x of e.attributes) a[x.name] = String(x.value).slice(0, 45);
            out.push({tag: e.tagName, text: t,
                      size: Math.round(r.width) + 'x' + Math.round(r.height),
                      pos: Math.round(r.x) + ',' + Math.round(r.y), attrs: a});
          });
          return out;
        }""")
        for s in spots[:20]:
            print("   ", s)
        if not spots:
            print("   none found by text -- the spots are probably SVG paths;")
            print("   re-run with --secs 40, or dump [data-e2e='bet-grid'] children.")

        shot = f"shots/starexch-table-{time.strftime('%Y%m%d-%H%M%S')}.png"
        try:
            target.screenshot(path=shot)
            print("\nscreenshot:", shot)
        except Exception:
            pass
    finally:
        try:
            context.close()
        finally:
            browser.close()
