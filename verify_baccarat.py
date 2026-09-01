"""Read-only end-to-end check of the Baccarat hedge path -- the gameplay bot's
counterpart to verify_stockmarket.py.

Drives the REAL production helper (_open_table_for with game=BACCARAT), so it
exercises whatever route the site actually needs (cricmatch's lobby + tile,
starexch's direct lobby URL + goToCasinoLive tile, winclash's /casinoRedirect
launch URL and its AWS WAF interstitial) exactly as run_paired_hedge would --
but places NO bets and clicks nothing inside the game beyond navigation.

Run this before ever calling /run on a gameplay bot, and especially on a site
whose Baccarat table this engine has never opened before: it confirms the
login, that the table id in SiteProfile.casino_game_ids really is Baccarat,
that both bet spots resolve, what the table's chip rail (and therefore its
minimum stake) actually is, and that the betting-window detector fires.

Usage:
  .venv/bin/python verify_baccarat.py <username> <password>
        [--url https://winclash.com/] [--proxy h:p:u:p] [--secs 120]
        [--tile "Baccarat A"]
"""
import argparse
import time

import main as m
from sites.games import BACCARAT

parser = argparse.ArgumentParser()
parser.add_argument("username")
parser.add_argument("password")
parser.add_argument("--url", default=None)
parser.add_argument("--proxy", default=None)
parser.add_argument("--secs", type=int, default=120)
parser.add_argument("--tile", default=BACCARAT.tile_text,
                    help="table to open, as named in SiteProfile.casino_game_ids "
                         "or the site's own lobby (default: Baccarat A)")
args = parser.parse_args()

SITE_URL = args.url or m.SITE_URL
prof = m.profile_for(SITE_URL)

pw, browser = m._launch_pw_browser()
context = None
bridge = None
try:
    proxy_conf = m.parse_proxy(args.proxy) if args.proxy else None
    if proxy_conf:
        proxy_conf, bridge = m.maybe_bridge_proxy(proxy_conf)

    print(f"--- site {prof.key}: opening {args.tile!r} through the production helper ---")
    if prof.casino_launch_mode == "direct_game_url":
        gid = prof.casino_game_ids.get(args.tile)
        print(f"    launch mode: direct URL, game id {gid!r} "
              f"(from sites/{prof.key}.py casino_game_ids)")
    t0 = time.time()
    context, page, game_page, frame = m._open_table_for(
        browser, args.username, args.password, SITE_URL,
        BACCARAT.category, args.tile,
        proxy_conf=proxy_conf, proxy=args.proxy, progress=lambda s: print("   ", s),
        label="verify", game=BACCARAT)
    print(f"    took {time.time() - t0:.0f}s")
    print("    game url :", game_page.url[:130])
    # The single most important line on a site whose id was never driven: a
    # wrong id opens some other table perfectly happily.
    print("    table id :", m._table_id(game_page))

    print("\n--- readouts ---")
    print("    read_game_balance :", m.read_game_balance(frame))
    print("    _read_total_bet   :", m._read_total_bet(frame))
    rail = m.read_chips(frame)
    raw = rail.get("chips") or []
    # Baccarat's rail carries hidden 0-value TEMPLATE nodes alongside the real
    # chips (measured live on cricmatch: eighteen nodes, twelve of them 0).
    # Reporting min() over the raw list would say the table minimum is 0.
    chips = sorted({c for c in raw if c})
    print("    chip rail         :", chips, "selected:", rail.get("selected"),
          f"({len(raw) - len(chips)} zero-value template nodes ignored)"
          if len(raw) != len(chips) else "")
    if chips:
        print(f"    -> smallest real chip is {min(chips)}, so that is the "
              f"smallest stake a /run can place on this table (a side)")

    # Both bet spots must resolve, or a real run would half-place and trip the
    # partial-unhedged safety stop.
    for role in (BACCARAT.side_a_role, BACCARAT.side_b_role):
        found = frame.evaluate(
            '(r) => !!document.querySelector(`[data-role="${r}"]`)', role)
        print(f"    role {role:18} present: {found}")

    print(f"\n--- watching {args.secs}s for betting windows (no bets placed) ---")
    seen_open = 0
    prev = None
    deadline = time.time() + args.secs
    while time.time() < deadline:
        is_open = m._betting_open(frame, BACCARAT)
        if is_open != prev:
            print(f"  [{time.strftime('%H:%M:%S')}] betting_open={is_open}  "
                  f"total_bet={m._read_total_bet(frame)}")
            if is_open:
                seen_open += 1
            prev = is_open
        time.sleep(1)

    print(f"\n=== RESULT: {seen_open} betting window(s) detected in {args.secs}s ===")
    print("PASS" if seen_open >= 1 else
          "FAIL -- the window detector never fired; a real run would stall")
finally:
    for closer in (context,):
        try:
            if closer is not None:
                closer.close()
        except Exception:
            pass
    try:
        browser.close()
    except Exception:
        pass
    try:
        pw.stop()
    except Exception:
        pass
    m.stop_bridge(bridge)
