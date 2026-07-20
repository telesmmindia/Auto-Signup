"""Read-only end-to-end check of the Stock Market Live engine path.

Drives the REAL production helper (_open_table_for with game=STOCKMARKET), so
it exercises the provider-lobby hop, the per-game table-ready probe, and the
instruction-based betting-window detector exactly as run_paired_hedge would --
but places NO bets and clicks nothing inside the game beyond navigation.

Run this before ever calling /run on the stockmarket bot.

Usage: .venv/bin/python verify_stockmarket.py <username> <password> [--secs N]
"""
import argparse
import time

from playwright.sync_api import sync_playwright

import main as m
from sites.games import STOCKMARKET

parser = argparse.ArgumentParser()
parser.add_argument("username")
parser.add_argument("password")
parser.add_argument("--url", default=None)
parser.add_argument("--proxy", default=None)
parser.add_argument("--secs", type=int, default=200)
args = parser.parse_args()

SITE_URL = args.url or m.SITE_URL

pw, browser = m._launch_pw_browser()
context = None
bridge = None
try:
    proxy_conf = m.parse_proxy(args.proxy) if args.proxy else None
    if proxy_conf:
        proxy_conf, bridge = m.maybe_bridge_proxy(proxy_conf)

    print("--- opening the Stock Market table through the production helper ---")
    t0 = time.time()
    context, page, game_page, frame = m._open_table_for(
        browser, args.username, args.password, SITE_URL,
        STOCKMARKET.category, STOCKMARKET.tile_text,
        proxy_conf=proxy_conf, progress=lambda s: print("   ", s),
        label="verify", game=STOCKMARKET)
    print(f"    took {time.time() - t0:.0f}s")
    print("    game url :", game_page.url[:130])
    print("    table id :", m._table_id(game_page))

    print("\n--- readouts ---")
    print("    read_game_balance :", m.read_game_balance(frame))
    print("    read_portfolio    :", m.read_portfolio(frame, STOCKMARKET))
    print("    _read_total_bet   :", m._read_total_bet(frame))
    print("    instruction       :", repr(m._read_instruction(frame)))
    print("    _cashout_ready    :", m._cashout_ready(frame, STOCKMARKET))

    # Both bet spots must resolve, or a real run would half-place and trip the
    # partial-unhedged safety stop.
    for role in (STOCKMARKET.side_a_role, STOCKMARKET.side_b_role,
                 STOCKMARKET.cashout_role):
        found = frame.evaluate(
            '(r) => !!document.querySelector(`[data-role="${r}"]`)', role)
        print(f"    role {role:12} present: {found}")

    print(f"\n--- watching {args.secs}s for betting windows (no bets placed) ---")
    seen_open = 0
    prev = None
    deadline = time.time() + args.secs
    while time.time() < deadline:
        is_open = m._betting_open(frame, STOCKMARKET)
        if is_open != prev:
            instr = m._read_instruction(frame)
            print(f"  [{time.strftime('%H:%M:%S')}] betting_open={is_open}  "
                  f"instruction={instr!r}  portfolio={m.read_portfolio(frame, STOCKMARKET)}")
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
