#!/usr/bin/env python3
"""Read-only probe for the jeetwin exchange white-label (playinmatch99).

Places NO bet. It drives exactly the code the bot drives -- it builds a real
exchange.AccountSession and calls the real site adapter -- so a pass here
means the bot's own path works, not merely that the site is up.

    .venv/bin/python probe_playinmatch.py --user <id> --pass <pw>
    .venv/bin/python probe_playinmatch.py --user <id> --pass <pw> \
        --url https://playinmatch99.com --sport cricket

What it checks, in order:
  1. login            POST /api2/v2/login          (email/password/digits)
  2. balance          POST /api2/v2/getBalance
  3. match list       GET  /mobile/sports          (flat, no series level)
  4. event markets    GET  /mobile/eventDetails/<id>?type=<n>
  5. live odds        POST odds.funoddsin.com/.../getMarketBook/<eventId>
  6. unmatched bets   POST /api2/v2/getBetsHistory

Steps 3-5 work anonymously and were verified live 2026-10-28. Steps 1, 2
and 6 need a real account -- they are coded from the site's own inline JS
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


SITE_URL = _arg("--url", "https://playinmatch99.com").rstrip("/")
USERNAME = _arg("--user", os.environ.get("EXCHANGE_PROBE_USER", ""))
PASSWORD = _arg("--pass", os.environ.get("EXCHANGE_PROBE_PASS", ""))
SPORT = _arg("--sport", "cricket")
EVENT = _arg("--event", "")

if not USERNAME or not PASSWORD:
    sys.exit("Pass --user <id> --pass <pw> (or set EXCHANGE_PROBE_USER/PASS).")

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
    if type(bot.SITE).__name__ != "PlayinmatchSite":
        print("\n⚠️  That URL did not select the jeetwin adapter. Either it is "
              "a different platform, or its hostname needs a line in "
              "exchange_sites._BY_HOST.")

    head(1, "login")
    acc = bot.AccountSession(USERNAME, PASSWORD)
    if not acc.login():
        print(f"❌ {acc.error}")
        print("\nNothing below can run without a session. If the message is a "
              "refusal from the app, the credentials are wrong; if it is an "
              "edge block, try again from another IP.")
        return 1
    print(f"✅ logged in (proxy: {acc.proxy or 'direct'})")

    head(2, "balance")
    print(f"balance  {acc.balance}")
    print(f"exposure {acc.exposure}")
    if acc.balance is None:
        print("⚠️  balance came back empty -- check getBalance's reply shape "
              "in the log above before trusting /ids or /refresh.")

    head(3, f"match list ({SPORT})")
    matches = bot.SITE.list_matches(acc, SPORT, None)
    print(f"{len(matches)} matches")
    for m in matches[:5]:
        print(f"  {m['event_id']}  {m['name']}")
    if not matches and not EVENT:
        print("❌ no matches listed; pass --event <id> to probe one directly.")
        return 1

    event_id = EVENT or matches[0]["event_id"]
    ev_type = bot.SITE.SPORT_EVENT_TYPE.get(SPORT, "4")

    head(4, f"event page {bot.SITE.event_path(event_id, ev_type)}")
    parsed = bot.SITE.load_event(acc, event_id, ev_type)
    print(f"limit blob: {len(parsed['limit'])} chars "
          f"{'✅' if parsed['limit'] else '❌ MISSING -- placeBet needs it'}")
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

    head(6, "unmatched bets")
    ids = bot.SITE.unmatched_ids(acc)
    print(f"{len(ids)} unmatched: {ids[:10]}")

    print(f"\n{'=' * 64}\n✅ Read-only probe finished. No bet was placed.\n"
          f"{'=' * 64}\n"
          "Still unproven: placeBet and cancelAllBet. Run the bot's confirm\n"
          "screen 🧪 Test 1 ID (an unmatched BACK @1.01, cancelled at once)\n"
          "on a funded ID before any multi-ID bet.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
