"""What tables does Evolution's in-game lobby carry, and what do they cost?
Read-only, places no bets.

The knockout's speed and its leftovers are both set by the table's smallest
chip: on Baccarat A (chips 100/500/...) an account holding 160 can only stake
100, so it takes extra hands to drain and leaves up to Rs 99 stranded when it
is knocked out. A table whose smallest chip is Rs 10 drains a loser in ONE
hand and strands at most Rs 9. This probe finds such a table.

Two modes, both starting from one real seat on Baccarat A (the entry game):

  default          type each --search term into the lobby's Search box and
                   dump every result tile's full text (name + any stake range
                   the tile shows). No table is opened.

  --open "<tile>"  actually switch to that table via the lobby (the same hop
                   the Stock Market hedge uses), then sit read-only for
                   --secs sampling: the chip rail's real denominations, the
                   betting-window timer, and the BET LIMITS text. Everything
                   needed to set TOURNAMENT_LOBBY_TILE / TOURNAMENT_TABLE_MIN
                   with confidence instead of guesses.

Usage (uses the tournament sheet's own accounts; nothing typed or printed):
    .venv/bin/python probe_lobby_tables.py --env .env.starexch.tournament
    .venv/bin/python probe_lobby_tables.py --env .env.starexch.tournament \
        --open "Speed Baccarat A" --secs 90
"""
import argparse
import sys
import time
from dataclasses import replace

_env_file = None
if "--env" in sys.argv:
    _env_file = sys.argv[sys.argv.index("--env") + 1]

import main as m  # noqa: E402

if _env_file:
    from dotenv import load_dotenv
    load_dotenv(_env_file, override=True)

import os  # noqa: E402

import tournament as t  # noqa: E402
import tournament_runner as R  # noqa: E402
from sites.games import BACCARAT  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--env", default=None)
ap.add_argument("--search", nargs="*",
                default=["Baccarat", "Speed", "Dragon", "Andar", "Teen Patti"],
                help="terms to type into the lobby search, one dump each")
ap.add_argument("--open", default=None,
                help="switch to this exact tile and sample it read-only")
ap.add_argument("--secs", type=float, default=90,
                help="how long to sample the opened table")
ap.add_argument("--account", type=int, default=0,
                help="which sheet row's account to seat (0 = first)")
ap.add_argument("--direct", action="store_true",
                help="skip the configured proxies")
args = ap.parse_args()

SITE_URL = os.environ.get("BOT_SITE_URL") or m.SITE_URL
proxies = [None] if args.direct else R.current_proxies()

ws = R.open_worksheet()
roster = R.roster_from_sheet(ws)
if not roster:
    sys.exit("the sheet has no usable USERNAME/PASSWORD rows")
acct = roster[min(args.account, len(roster) - 1)]

print(f"site  : {SITE_URL}")
print(f"seat  : {acct['username']} (read-only, no bets)")
print(f"mode  : " + (f"open {args.open!r} and sample {args.secs:.0f}s"
                     if args.open else f"dump search results for {args.search}"))
print()

seat = t.Seat(acct["username"], acct["password"], site_url=SITE_URL,
              proxy=proxies[0] if proxies else None)
fut = seat.open_async(progress=lambda msg: print(f"   {msg}"))
try:
    fut.result(timeout=600)
except Exception as exc:
    seat.close()
    sys.exit(f"could not seat {acct['username']}: {exc}")
print(f"   seated at table {seat.table_id}, balance {seat.balance}\n")


# Everything below runs on the seat's own thread via seat.call() -- Playwright
# thread affinity, same as the tournament itself.

DUMP_TILES_JS = """(term) => {
    // A lobby tile is a moderately sized clickable box whose text mentions
    // the search term. Dump each one's FULL text -- Evolution tiles carry the
    // table name and, on most skins, the min stake -- plus any data-* attrs.
    const seen = new Set();
    const out = [];
    const all = Array.from(document.querySelectorAll('*'));
    for (const e of all) {
        const r = e.getBoundingClientRect();
        if (r.width < 90 || r.width > 460 || r.height < 60 || r.height > 460)
            continue;
        const txt = (e.innerText || '').trim();
        if (!txt || txt.length > 220) continue;
        if (!txt.toUpperCase().includes(term.toUpperCase())) continue;
        // keep only the OUTERMOST such element (tiles nest their label)
        let p = e.parentElement, outermost = true;
        while (p) {
            const pr = p.getBoundingClientRect();
            const pt = (p.innerText || '').trim();
            if (pr.width <= 460 && pr.height <= 460 && pt.length <= 220
                && pt.toUpperCase().includes(term.toUpperCase())) {
                outermost = false; break;
            }
            p = p.parentElement;
        }
        if (!outermost) continue;
        const key = txt + '|' + Math.round(r.left) + '|' + Math.round(r.top);
        if (seen.has(key)) continue;
        seen.add(key);
        const attrs = {};
        for (const a of e.attributes || [])
            if (a.name.startsWith('data-')) attrs[a.name] = a.value.slice(0, 60);
        out.push({text: txt.replace(/\\n/g, ' | '), attrs});
    }
    return out;
}"""


def open_lobby():
    """Click the LOBBY button and return the lobby frame."""
    deadline = time.time() + 45
    while time.time() < deadline:
        try:
            loc = seat.frame.locator('[data-role="lobby-button"]').first
            if loc.count() and loc.is_visible():
                loc.click(timeout=8000, force=True)
                break
        except Exception:
            pass
        seat.game_page.wait_for_timeout(500)
    lobby = m._find_provider_lobby_frame(seat.game_page, timeout_ms=15000)
    if lobby is None:
        raise RuntimeError("the provider lobby frame never appeared")
    return lobby


def dump_searches(terms):
    lobby = open_lobby()
    for term in terms:
        try:
            box = lobby.get_by_placeholder("Search").first
            box.click(timeout=6000)
            box.fill("")
            box.type(term, delay=120)
        except Exception as exc:
            print(f"-- {term}: could not use the search box ({exc})")
            continue
        seat.game_page.wait_for_timeout(4500)
        try:
            tiles = lobby.evaluate(DUMP_TILES_JS, term)
        except Exception as exc:
            tiles = []
            print(f"-- {term}: dump failed ({exc})")
        print(f"-- search {term!r}: {len(tiles)} tile(s)")
        for tl in tiles:
            attrs = " ".join(f"{k}={v}" for k, v in (tl.get("attrs") or {}).items())
            print(f"     {tl['text']}" + (f"   [{attrs}]" if attrs else ""))
        print()


SAMPLE_TABLE_JS = """() => {
    const g = r => document.querySelector(`[data-role="${r}"]`);
    const txt = e => e ? (e.innerText || '').trim() : null;
    const chips = Array.from(document.querySelectorAll(
        '[data-role="chip"][data-value]')).map(e => ({
            v: e.getAttribute('data-value'),
            w: Math.round(e.getBoundingClientRect().width),
            cur: getComputedStyle(e).cursor}));
    const limits = Array.from(document.querySelectorAll(
        '[data-role*="bet-limit"],[data-role*="limits"]'))
        .map(e => (e.innerText || '').trim().replace(/\\s+/g, ' ').slice(0, 160))
        .filter(Boolean);
    const roles = {};
    for (const e of document.querySelectorAll('[data-role]')) {
        const r = e.getBoundingClientRect();
        if (r.width > 0 && r.height > 0)
            roles[e.getAttribute('data-role')] = true;
    }
    return {
        timer: !!g('circle-timer'),
        chips,
        bal: txt(g('balance-label-value')),
        totalBet: txt(g('total-bet-label-value')),
        limits,
        roles: Object.keys(roles),
    };
}"""


def sample_table(tile, secs):
    game = replace(BACCARAT, via_provider_lobby=True,
                   lobby_search=tile, lobby_tile=tile)
    print(f"-- switching to {tile!r} via the lobby…")
    seat.frame = m._open_via_provider_lobby(seat.game_page, seat.frame, game)
    tid = m._table_id(seat.game_page)
    print(f"   switched, table_id={tid}\n   sampling {secs:.0f}s…\n")

    windows = 0
    n = 0
    rails = {}
    limits_seen = set()
    roles_seen = {}
    deadline = time.time() + secs
    while time.time() < deadline:
        try:
            smp = seat.frame.evaluate(SAMPLE_TABLE_JS)
        except Exception as exc:
            print(f"   sample failed: {str(exc)[:80]}")
            time.sleep(1)
            continue
        n += 1
        if smp.get("timer"):
            windows += 1
        live = sorted({int(c["v"]) for c in smp.get("chips") or []
                       if c.get("cur") == "pointer" and c.get("v")})
        if live:
            rails[tuple(live)] = rails.get(tuple(live), 0) + 1
        for L in smp.get("limits") or []:
            limits_seen.add(L)
        for r in smp.get("roles") or []:
            roles_seen[r] = roles_seen.get(r, 0) + 1
        time.sleep(1)

    print(f"   samples          : {n}")
    print(f"   window open      : {windows} ({windows * 100 // max(1, n)}%)")
    if rails:
        for rail, cnt in sorted(rails.items(), key=lambda kv: -kv[1]):
            print(f"   clickable rail   : {list(rail)}  (seen {cnt}x)")
        smallest = min(min(r) for r in rails)
        print(f"\n   => smallest chip Rs {smallest}."
              f" Configure:\n"
              f"      TOURNAMENT_LOBBY_TILE={tile}\n"
              f"      TOURNAMENT_TABLE_MIN={smallest}")
    else:
        print("   clickable rail   : never seen -- do not use this table")
    if limits_seen:
        print("   bet-limits text  :")
        for L in sorted(limits_seen):
            print(f"      {L}")
    print(f"   balance readable : {smp.get('bal')!r}")
    if roles_seen:
        print("   data-roles seen (count of samples each was visible in):")
        for r, cnt in sorted(roles_seen.items()):
            print(f"      {cnt:>4}  {r}")


try:
    if args.open:
        seat.call(sample_table, args.open, args.secs).result(
            timeout=args.secs + 300)
    else:
        seat.call(dump_searches, args.search).result(timeout=600)
finally:
    seat.close()
    print("\ndone (no bets were placed)")
