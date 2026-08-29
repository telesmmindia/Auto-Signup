"""List winclash.com's live-casino catalogue, so a new table can be added to
sites/winclash.py's `casino_game_ids` by reading its real id rather than
guessing one. Read-only: logs in, asks the site's own /casinoGamesList
endpoint, prints matches. Opens no table and places no bet.

winclash launches a table by URL -- /casinoRedirect?q=<id>&provider=<prov>
&type=casino -- so the id below is the only thing needed to drive a new one
(and only baccarat-family/roulette tables have a GameProfile that fits; see
sites/games.py before pointing the engine at something new).

Usage:
  .venv/bin/python probe_winclash_games.py <user> <pass> [--find baccarat]
        [--provider evolution] [--proxy h:p:u:p] [--headed]
"""
import argparse
import json
import re

from playwright.sync_api import sync_playwright

import main as engine

SITE = "https://winclash.com/"

parser = argparse.ArgumentParser()
parser.add_argument("username")
parser.add_argument("password")
parser.add_argument("--find", default="", help="only show names matching this")
parser.add_argument("--provider", default="evolution")
parser.add_argument("--proxy", default=None)
parser.add_argument("--headed", action="store_true")
args = parser.parse_args()

prof = engine.profile_for(SITE)
proxy_conf = engine.parse_proxy(args.proxy) if args.proxy else None
bridge = None
if proxy_conf:
    proxy_conf, bridge = engine.maybe_bridge_proxy(proxy_conf)

with sync_playwright() as pw:
    browser = pw.chromium.launch(headless=not args.headed,
                                 args=engine._ANTI_THROTTLE_ARGS)
    ctx = engine.new_site_context(browser, SITE, proxy_conf=proxy_conf)
    page = ctx.new_page()
    try:
        page.goto(SITE, wait_until="domcontentloaded", timeout=90000)
        page, ok, msg = engine.ensure_waf_cleared(page, SITE, proxy=args.proxy,
                                                  proxy_conf=proxy_conf)
        print(f"WAF: {ok} {msg}")
        if not ok:
            raise SystemExit(1)
        ctx = page.context
        outcome, msgs = engine.login(page, args.username, args.password,
                                     site_url=SITE, already_loaded=True)
        print(f"login: {outcome} {'; '.join(msgs)}")
        if outcome != "ok":
            raise SystemExit(1)

        # Settle on the lobby first. The login redirect can still be in
        # flight right after login() returns, and a fetch() issued into a
        # navigating page fails with a bare "Failed to fetch".
        page.goto(SITE + "live-casino", wait_until="domcontentloaded",
                  timeout=90000)
        engine.wait_out_waf_wall(page, 25)
        page.wait_for_timeout(6000)

        # Page through the site's own catalogue endpoint. It is paginated and
        # ignores a per-page size, so walk pages until one comes back empty.
        rows = page.evaluate("""async (provider) => {
            const m = document.querySelector('meta[name=csrf-token]');
            const tk = m ? m.content : '';
            const out = [];
            for (let p = 1; p <= 30; p++) {
              const r = await fetch('/casinoGamesList', {method: 'POST',
                credentials: 'same-origin',
                headers: {'Content-Type':
                            'application/x-www-form-urlencoded; charset=UTF-8',
                          'X-Requested-With': 'XMLHttpRequest',
                          'X-CSRF-TOKEN': tk},
                body: 'page=' + p + '&category=all&groupedgames_count=6'
                      + '&provider=' + provider + '&name=all&screen_width=1280'
                      + '&_token=' + encodeURIComponent(tk)});
              let d;
              try { d = await r.json(); } catch (e) { break; }
              const rows = d.data || [];
              if (!rows.length) break;
              for (const g of rows) out.push({id: g.id, name: g.name,
                                              provider: g.provider,
                                              cls: g.class_name});
              if (d.current_page >= d.last_page) break;
            }
            return out;
        }""", args.provider)

        pat = re.compile(args.find, re.I) if args.find else None
        shown = [g for g in rows if not pat or pat.search(str(g["name"]))]
        print(f"\n{len(rows)} {args.provider} tiles, {len(shown)} shown"
              + (f" (matching {args.find!r})" if args.find else ""))
        for g in sorted(shown, key=lambda g: str(g["name"])):
            print(f"  id={str(g['id']):6} {str(g['name'])[:46]:46} "
                  f"{str(g['cls'])[:44]}")
        known = prof.casino_game_ids
        print("\nAlready in sites/winclash.py:", json.dumps(known))
        print("To add one, put its id in casino_game_ids and give it a "
              "GameProfile in sites/games.py if it is not baccarat-family.")
    finally:
        try:
            ctx.close()
        except Exception:
            pass
        browser.close()
        engine.stop_bridge(bridge)
