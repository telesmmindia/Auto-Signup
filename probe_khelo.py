#!/usr/bin/env python3
"""Read-only probe for the khelo family (the jeetwin template's DESKTOP routes).

Places NO bet. It drives exactly the code the bot drives -- it builds a real
exchange.AccountSession and calls the real site adapter -- so a pass here
means the bot's own path works, not merely that the site is up.

    .venv/bin/python probe_khelo.py --url https://khelo24bet7.com \
        --user <id> --pass <pw>

Without --user/--pass it runs the anonymous half only, which is still the
quickest way to tell whether a new site on this template is reachable and
parses:

    .venv/bin/python probe_khelo.py --url https://khelosports77.com

What it checks, in order:
  1. login         POST /api2/login            {_token, email, password}
  2. balance       POST /api/getBalance
  3. match list    GET  /ex/d/inplay           (flat, no series level)
  4. event page    GET  /ex/d/details/<id>     markets, runners, limit blob
  5. live odds     POST <odds host>/api/v1/get/getMarketBook/<eventId>
  6. open bets     GET  /ex/d/api/getOpenBets/<marketId>   (HTML)

Steps 3-5 work anonymously and were verified live 2026-11-03. Steps 1, 2
and 6 need a real account -- they are coded from the sites' own inline JS
and have never been run. This probe is how you find out before betting.

Credentials are arguments, never literals: this repo is public.
"""
import os
import sys


def _arg(flag, default=""):
    if flag in sys.argv:
        i = sys.argv.index(flag)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return default


SITE_URL = _arg("--url", "https://khelo24bet7.com").rstrip("/")
USERNAME = _arg("--user", os.environ.get("EXCHANGE_PROBE_USER", ""))
PASSWORD = _arg("--pass", os.environ.get("EXCHANGE_PROBE_PASS", ""))
SPORT = _arg("--sport", "cricket")
EVENT = _arg("--event", "")

# exchange.py reads its config at import. Point it at this site and give it
# a token placeholder -- nothing here starts the Telegram side.
os.environ["EXCHANGE_SITE_URL"] = SITE_URL
os.environ.setdefault("EXCHANGE_BOT_TOKEN", "0:probe")
os.environ.setdefault("MASTER_ADMIN_ID", "0")
sys.argv = [sys.argv[0], "--env", ".env.probe.nonexistent"]

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import exchange as bot          # noqa: E402


def head(n, text):
    print(f"\n{'=' * 64}\n{n}. {text}\n{'=' * 64}")


def main():
    print(f"site     {SITE_URL}")
    print(f"adapter  {type(bot.SITE).__name__}  (has_series={bot.SITE.has_series})")
    print(f"odds     {bot.SITE.odds_host}")
    if type(bot.SITE).__name__ != "Khelo24Site":
        print("\n⚠️  That URL did not select the khelo adapter. Either it is "
              "a different platform, or its hostname needs a line in "
              "exchange_sites._BY_HOST.")

    acc = bot.AccountSession(USERNAME or "anon", PASSWORD or "anon")
    anon = not (USERNAME and PASSWORD)

    if anon:
        print("\n(no --user/--pass: running the anonymous half only -- "
              "match list, event page and odds)")
        # Take a proxy the same way a real session does; these sites can be
        # WAF-walled from a datacenter IP, which reads as "site blocked the
        # page" rather than as a proxy problem.
        acc.proxy = bot.next_proxy()
        acc.session = bot.engine.http_session_for(acc.proxy)
        acc.csrf = ""
        print(f"(proxy: {acc.proxy or 'direct'})")
    else:
        head(1, "login")
        # Go through AccountSession.login(), not SITE.login(acc) -- the
        # bot's own method is what assigns the proxy and follows a
        # sister-domain hand-off, so calling the adapter directly probes a
        # path the bot never takes (and goes out direct, unproxied).
        ok = acc.login()
        msg = acc.error or "Login Successfully"
        print(f"{'✅' if ok else '❌'} {msg}   (proxy: {acc.proxy or 'direct'})")
        if not ok:
            print("\nNothing that needs a session can run. If the message is "
                  "a refusal from the app, the credentials are wrong; if it "
                  "is an edge block, try again from another IP.")
            return 1

        head(2, "balance")
        bal = bot.SITE.read_balance(acc)
        print(f"balance  {(bal or {}).get('balance')}")
        print(f"exposure {(bal or {}).get('exposure')}")
        if not bal:
            print("⚠️  balance came back empty -- check getBalance's reply "
                  "shape before trusting /ids or /refresh.")

    head(3, f"match list ({SPORT})")
    matches = bot.SITE.list_matches(acc, SPORT, None)
    print(f"{len(matches)} matches")
    for m in matches[:6]:
        print(f"  {m['event_id']}  {m['name']}")
    if not matches and not EVENT:
        print("❌ no matches listed; pass --event <id> to probe one directly, "
              "or try --sport football/tennis (this list is live events only).")
        return 1

    event_id = EVENT or matches[0]["event_id"]
    ev_type = bot.SITE.SPORT_EVENT_TYPE.get(SPORT, "4")

    head(4, f"event page {bot.SITE.event_path(event_id, ev_type)}")
    parsed = bot.SITE.load_event(acc, event_id, ev_type)
    print(f"limit blob: {len(parsed['limit'])} chars "
          f"{'✅' if parsed['limit'] else '❌ MISSING -- placeBet needs it'}"
          f"   ({len(parsed.get('limits') or [])} distinct on the page)")
    for mk in parsed["markets"]:
        print(f"  {mk['market_id']}  {mk['market_name']}  "
              f"({len(mk['runners'])} runners)")
    market = parsed["match_odds"]
    if not market:
        print("❌ no Match Odds market here (match not started, or ended).")
        return 1
    print(f"\nMatch Odds: {market['teams']}")
    for rid, name in market["runners"].items():
        print(f"  {rid}  {name}")

    head(5, "live odds")
    raw = acc.get_market_book(
        market["event_type"], [market["market_id"]],
        referer=f"{SITE_URL}{bot.SITE.event_path(event_id, ev_type)}",
        event_id=market["event_id"] or event_id)
    book = bot.SITE.parse_odds(raw).get(market["market_id"]) or {}
    print(f"status    {book.get('status')}")
    print(f"hash      {'present' if book.get('hash') else 'MISSING'} "
          f"(goes into placeBet as `matched`)")
    print(f"liquidity {book.get('liquidity')}  (goes into placeBet as `vol`)")
    for rid, name in market["runners"].items():
        rr = (book.get("runners") or {}).get(str(rid), {})
        print(f"  {name:28} back {rr.get('back')}  lay {rr.get('lay')}")
    if book.get("status") != "OPEN":
        print("\n⚠️  Market is not OPEN, so its prices are blank. That is "
              "normal between overs -- try another match for a full read.")

    if anon:
        print(f"\n{'=' * 64}\n✅ Anonymous probe finished. No bet was placed.\n"
              f"{'=' * 64}\n"
              "Re-run with --user/--pass to exercise login, balance and the\n"
              "open-bets panel, which have never been run on this platform.")
        return 0

    head(6, "open bets")
    ids = bot.SITE.unmatched_ids(acc)
    print(f"{len(ids)} unmatched on the markets this session has opened: {ids[:10]}")
    print("(this platform lists open bets PER MARKET, so an empty list here "
          "means nothing unmatched on the event above -- not necessarily "
          "nothing on the account)")

    print(f"\n{'=' * 64}\n✅ Read-only probe finished. No bet was placed.\n"
          f"{'=' * 64}\n"
          "Still unproven: placeBet and cancelAllBet. Run the bot's confirm\n"
          "screen 🧪 Test 1 ID (an unmatched BACK @1.01, cancelled at once)\n"
          "on a funded ID before any multi-ID bet.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
