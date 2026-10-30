"""Read-only discovery of the Mines game client -- the script that has to run
before any Mines bot is trusted with a stake.

WHY THIS EXISTS, and why it is not just verify_baccarat.py with a new tile:

* Every game this repo drives so far is the EVOLUTION client on
  ezugi.evo-games.com, whose markup (data-role="bet-spot-...", "circle-timer",
  "balance-label-value") the engine already knows. Mines is NOT that. A client
  screen recording (2026-10-29) shows funcrash launching Mines into
  **ibegaming.com** -- a different provider with an entirely different
  vocabulary, the same way starexch's Ezugi client is a second game driver
  this repo deliberately does not have.
* main._open_game_direct() waits for the literal string "evo-games.com" in the
  game tab's URL and calls anything else a refused launch, so the production
  path cannot even reach this table. That is a real engine limit, not a
  configuration gap, and it is one of the things this probe is here to confirm
  before it gets changed.
* funcrash's casino_game_ids holds Evolution tables only. Mines' id and its
  provider name are unknown, so this asks the site's own /casinoGamesList
  rather than guessing -- the same rule probe_winclash_games.py follows.

WHAT THE RECORDING SHOWS, i.e. what the bot will need to drive (all of this is
read off a phone screen recording, so treat it as a description of the UI, not
as confirmed selectors -- confirming them is this script's whole job):

    a "Mines: N" dropdown           top left, N was set to 1
    a "Next: 1.01x" badge           top right, the next-reveal multiplier
    a 5x5 grid of covered tiles
    a "Bet INR" number input        with -, stack and + buttons
    a green "BET" button            becomes a "CASH OUT  <amount> INR" button
    a "RANDOM" button and an "Auto Game" toggle
    "Auto cashout in: N"            an idle countdown that ends the round by
                                    itself at 1.00x when no tile is revealed
    a balance, rendered as "1,000.00 INR" in the client's own header

PLACES NO BET. It never clicks BET, a tile, RANDOM, or the Auto Game toggle.
It logs in, opens the table, dumps what is there and leaves.

Usage:
  .venv/bin/python probe_mines.py <username> <password>
        [--url https://funcrash.com/] [--proxy h:p:u:p]
        [--name Mines] [--game-id 123] [--provider spribe]
        [--secs 60] [--headed]
"""
import argparse
import json
import time
from urllib.parse import urlsplit

import main as m

parser = argparse.ArgumentParser()
parser.add_argument("username", nargs="?", default=None)
parser.add_argument("password", nargs="?", default=None)
parser.add_argument("--from-pairs", default=None, metavar="FILE",
                    help="read credentials out of a bot PAIRS_FILE instead of "
                         "the command line, so they never reach a shell "
                         "history or a process list "
                         "(e.g. --from-pairs pairs.funcrash.gameplay.json)")
parser.add_argument("--pair", default=None,
                    help="which pair id in --from-pairs (default: the first)")
parser.add_argument("--side", default="banker", choices=["banker", "player"],
                    help="which side of that pair to log in as")
parser.add_argument("--url", default=None)
parser.add_argument("--proxy", default=None)
parser.add_argument("--name", default="Mines",
                    help="game name to look for in the site's catalogue")
parser.add_argument("--game-id", default=None,
                    help="skip the catalogue lookup and launch this id")
parser.add_argument("--provider", default=None,
                    help="launch with this provider (default: whatever the "
                         "catalogue says, else the profile's)")
parser.add_argument("--secs", type=int, default=60,
                    help="how long to watch the table after it loads")
parser.add_argument("--headed", action="store_true")
args = parser.parse_args()

if args.from_pairs:
    # Deliberately loaded here rather than passed in: pairs.*.json already
    # holds these in plaintext for the bot, so reading them is no new
    # exposure, whereas putting a live password on a command line is.
    with open(args.from_pairs) as fh:
        pairs = json.load(fh).get("pairs", {})
    if not pairs:
        raise SystemExit(f"{args.from_pairs} holds no pairs")
    pid = args.pair or sorted(pairs, key=lambda k: int(k) if k.isdigit() else k)[0]
    if pid not in pairs:
        raise SystemExit(f"no pair {pid!r} in {args.from_pairs} "
                         f"(have: {', '.join(sorted(pairs))})")
    side = pairs[pid][args.side]
    args.username, args.password = side["username"], side["password"]
    print(f"--- credentials: pair {pid} {args.side} "
          f"({args.username}) from {args.from_pairs} ---")
elif not (args.username and args.password):
    raise SystemExit("give a username and password, or --from-pairs FILE")

SITE_URL = args.url or m.SITE_URL
prof = m.profile_for(SITE_URL)
SHOTS = "shots"

def jdump(obj):
    return json.dumps(obj, indent=2, ensure_ascii=False, default=str)


# --- the catalogue -------------------------------------------------------
# Read out of the slots page's own loadGames(), confirmed live 2026-10-30:
# the games list is POST /homepageGames, NOT /casinoGamesList (that is the
# live-casino endpoint winclash uses, and it answers 419 here). It replies
# with JSON -- {groupedgames_list, gamelist:[{id, name, provider, link, ...}]}
# -- and it answers ANONYMOUSLY, so no login is needed just to resolve an id.
#
# The field that matters is `id`. The launch URL's `q` is that CATALOGUE ID,
# not the `link` slug: q=3139 opens the real client, while q=mines lands on
# launch.spribegaming.com and renders a permanently BLANK page. That one cost
# a session to find, so it is written down rather than left to be re-derived.
_CATALOGUE_JS = """async (term) => {
    const tok = (document.querySelector('meta[name=csrf-token]') || {}).content
             || (document.documentElement.innerHTML.match(/_token:'([^']+)'/) || [])[1]
             || '';
    const body = new URLSearchParams({
        page: '1', category: 'all', provider: '',
        perpage_count: '60', groupedgames_count: '0',
        gameSearch: term, _token: tok,
    });
    let res;
    try {
        res = await fetch('/homepageGames', {
            method: 'POST',
            headers: {'X-Requested-With': 'XMLHttpRequest',
                      'Content-Type': 'application/x-www-form-urlencoded'},
            body: body.toString(),
        });
    } catch (e) { return {error: String(e)}; }
    if (!res.ok) return {error: 'HTTP ' + res.status};
    let data;
    try { data = await res.json(); } catch (e) { return {error: 'not JSON'}; }
    return {rows: (data.gamelist || []).map(g => ({
        id: g.id, name: g.name, provider: g.provider, link: g.link}))};
}"""


def find_game(page, wanted):
    """Search the site's own catalogue once and return (id, provider, name).

    `wanted` matches the game NAME; --provider narrows it, which matters
    because a search for "mines" returns ~30 games from a dozen providers and
    only one of them is the client in the recording. Prints them all, because
    picking the wrong look-alike is the likely mistake here.
    """
    want = wanted.strip().lower()
    try:
        res = page.evaluate(_CATALOGUE_JS, want)
    except Exception as e:
        print(f"    catalogue could not be asked: {str(e)[:120]}")
        return None
    if res.get("error"):
        print(f"    catalogue: {res['error']}")
        return None
    rows = res.get("rows") or []
    print(f"    {len(rows)} games match {wanted!r}:")
    for r in rows:
        mark = "  <--" if (r.get("name") or "").strip().lower() == want else ""
        print(f"      {str(r.get('name'))[:26]:<28} id={str(r.get('id')):<7}"
              f" provider={str(r.get('provider')):<14} link={r.get('link')!r}{mark}")
    hits = [r for r in rows if want in (r.get("name") or "").lower()]
    if args.provider:
        hits = [r for r in hits if r.get("provider") == args.provider]
    exact = [r for r in hits if (r.get("name") or "").strip().lower() == want]
    pick = (exact or hits or [None])[0]
    if not pick:
        return None
    return str(pick["id"]), pick.get("provider"), pick.get("name")


# --- the dump ------------------------------------------------------------
# Everything a Mines driver could need a selector for, read off whatever
# document the game actually lives in. Deliberately broad: the point is to
# SEE the client's vocabulary, not to confirm a guess about it.
_DUMP_JS = """() => {
    const vis = (el) => {
        const r = el.getBoundingClientRect();
        const s = getComputedStyle(el);
        return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
    };
    const describe = (el) => {
        const r = el.getBoundingClientRect();
        const attrs = {};
        for (const a of el.attributes) {
            if (a.name === 'style') continue;
            attrs[a.name] = a.value.length > 80 ? a.value.slice(0, 80) + '…' : a.value;
        }
        return {
            tag: el.tagName.toLowerCase(),
            text: (el.innerText || el.value || '').trim().slice(0, 60).replace(/\\s+/g, ' '),
            attrs: attrs,
            box: [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)],
            visible: vis(el),
            disabled: el.disabled === true || el.getAttribute('aria-disabled') === 'true',
        };
    };
    const pick = (sel) => Array.from(document.querySelectorAll(sel)).map(describe);

    // Any element carrying a currency-looking number: the balance, the bet
    // field and the cash-out amount all read this way, and which node is
    // which is exactly what has to be learnt here.
    const money = [];
    for (const el of document.querySelectorAll('*')) {
        if (el.children.length) continue;
        const t = (el.innerText || '').trim();
        if (t && t.length < 32 && /[0-9][0-9,]*\\.?[0-9]*\\s*(INR|₹)|^[0-9,]+\\.[0-9]{2}$/.test(t)) {
            money.push(describe(el));
        }
    }

    // The grid. 25 same-sized siblings is the signature; report the biggest
    // group of equal-sized elements so the tile selector can be derived.
    const bySize = {};
    for (const el of document.querySelectorAll('div,button,span,td,rect,g')) {
        const r = el.getBoundingClientRect();
        if (r.width < 20 || r.height < 20 || r.width > 200) continue;
        const k = Math.round(r.width) + 'x' + Math.round(r.height);
        (bySize[k] = bySize[k] || []).push(el);
    }
    let grid = null;
    for (const [k, els] of Object.entries(bySize)) {
        if (els.length >= 20 && els.length <= 40 && (!grid || els.length > grid.els.length)) {
            grid = {size: k, els: els};
        }
    }

    return {
        url: location.href,
        title: document.title,
        buttons: pick('button,[role=button]').filter(d => d.visible).slice(0, 40),
        inputs: pick('input,select,textarea').slice(0, 25),
        selectOptions: Array.from(document.querySelectorAll('select')).map(s => ({
            attrs: Object.fromEntries(Array.from(s.attributes).map(a => [a.name, a.value])),
            options: Array.from(s.options).map(o => o.text.trim()),
            value: s.value,
        })),
        money: money.slice(0, 25),
        grid: grid ? {size: grid.size, count: grid.els.length,
                      sample: grid.els.slice(0, 3).map(describe)} : null,
        dataAttrs: Array.from(new Set(
            Array.from(document.querySelectorAll('*')).flatMap(el =>
                Array.from(el.attributes).filter(a => a.name.startsWith('data-'))
                     .map(a => a.name)))).slice(0, 60),
        bodyText: (document.body.innerText || '').trim().slice(0, 600),
    };
}"""


def dump_target(target, label):
    """Dump a page or frame. Returns the dict, or None if it could not run."""
    try:
        d = target.evaluate(_DUMP_JS)
    except Exception as e:
        print(f"    [{label}] could not be read: {str(e)[:120]}")
        return None
    print(f"\n--- {label}: {d['title']!r} ---")
    print(f"    {d['url'][:140]}")
    print(f"\n  buttons ({len(d['buttons'])}):")
    for b in d["buttons"]:
        ident = b["attrs"].get("id") or b["attrs"].get("class", "")[:40]
        print(f"    {b['text'][:28]:<30} {b['tag']:<7} {ident:<42}"
              f" {'DISABLED' if b['disabled'] else ''}")
    print(f"\n  inputs / selects ({len(d['inputs'])}):")
    for i in d["inputs"]:
        print(f"    {i['tag']:<9} {str(i['attrs'].get('type','')):<9}"
              f" value={i['text'][:18]:<20} {jdump(i['attrs'])[:90]}")
    for s in d["selectOptions"]:
        print(f"    select options: {s['options'][:12]} (value={s['value']!r})")
    print(f"\n  money-shaped text ({len(d['money'])}):")
    for mo in d["money"]:
        ident = mo["attrs"].get("class", "") or mo["attrs"].get("id", "")
        print(f"    {mo['text'][:22]:<24} {mo['tag']:<6} {ident[:60]}")
    print(f"\n  grid: {d['grid']['count'] if d['grid'] else 0} equal tiles"
          + (f" at {d['grid']['size']}" if d["grid"] else " -- none found"))
    if d["grid"]:
        print(f"    sample: {jdump(d['grid']['sample'][0])[:400]}")
    print(f"\n  data-* attribute names seen: {d['dataAttrs']}")
    print(f"\n  body text:\n    " + d["bodyText"].replace("\n", "\n    ")[:600])
    return d


# --- run -----------------------------------------------------------------
pw, browser = (None, None)
context = None
bridge = None
try:
    import playwright.sync_api as _pw_api
    pw = _pw_api.sync_playwright().start()
    browser = pw.chromium.launch(headless=not args.headed, args=m._ANTI_THROTTLE_ARGS)

    proxy_conf = m.parse_proxy(args.proxy) if args.proxy else None
    if proxy_conf:
        proxy_conf, bridge = m.maybe_bridge_proxy(proxy_conf)

    context = m.new_site_context(browser, SITE_URL, proxy_conf)
    page = context.new_page()

    print(f"--- site {prof.key} ({SITE_URL}) ---")
    page.goto(SITE_URL, wait_until="domcontentloaded", timeout=60000)
    if prof.waf_on_navigation:
        page, ok, why = m.ensure_waf_cleared(page, SITE_URL, proxy=args.proxy,
                                             proxy_conf=proxy_conf)
        context = page.context
        print(f"    WAF: {'cleared' if ok else 'STILL UP'} -- {why}")
        if not ok:
            raise SystemExit("the WAF was not cleared; set CAPSOLVER_API_KEY "
                             "or try another exit IP")

    print("\n--- logging in ---")
    t0 = time.time()
    outcome, msgs = m.login(page, args.username, args.password, SITE_URL,
                            already_loaded=True)
    print(f"    {outcome} in {time.time() - t0:.0f}s"
          + (f" -- {'; '.join(msgs)[:200]}" if msgs else ""))
    if outcome != "ok":
        raise SystemExit("login did not complete, so nothing below could be read")

    # --- find the game ---------------------------------------------------
    game_id, provider = args.game_id, args.provider
    if not game_id:
        print(f"\n--- looking for {args.name!r} in the site's own catalogue ---")
        found = find_game(page, args.name)
        if not found:
            raise SystemExit(
                f"no game named {args.name!r} in any provider tried. Re-run "
                f"with --provider <name> once you know it, or with --game-id.")
        game_id, provider, real_name = found
        print(f"\n    -> {real_name!r} is id {game_id} on provider {provider!r}")
    provider = provider or prof.casino_provider

    # --- launch it -------------------------------------------------------
    # NOTE the site's own slots pages build this with type=slots; type=casino
    # (the profile's default, used by the Evolution tables) reaches the same
    # client, so the profile template is left alone here.
    parts = urlsplit(SITE_URL)
    path = prof.casino_launch_path.format(id=game_id, provider=provider)
    launch = f"{parts.scheme}://{parts.netloc}{path}"
    print(f"\n--- launching {launch} ---")
    game_page = context.new_page()
    game_page.goto(launch, wait_until="domcontentloaded", timeout=60000)

    # Deliberately NOT waiting for "evo-games.com" the way
    # main._open_game_direct does -- which host this lands on is one of the
    # facts being established. Wait for it to leave the operator's domain.
    host0 = urlsplit(game_page.url).netloc
    for _ in range(30):
        game_page.wait_for_timeout(1000)
        host = urlsplit(game_page.url).netloc
        if host and host != parts.netloc:
            break
    host = urlsplit(game_page.url).netloc
    print(f"    landed on {host!r} (operator host is {parts.netloc!r})")
    if host == parts.netloc:
        body = ""
        try:
            body = (game_page.inner_text("body") or "").strip()[:300]
        except Exception:
            pass
        print(f"    the launch never left the operator -- page says: {body}")

    game_page.wait_for_timeout(8000)

    # --- dump it ---------------------------------------------------------
    # The recording shows Mines filling the whole tab, so the client may BE
    # the tab's document (like starexch's Ezugi client) rather than an iframe
    # (like Evolution's). Dump the tab and every frame in it; which one holds
    # the controls is the answer.
    print(f"\n=== TAB DOCUMENT ===")
    dump_target(game_page, "tab")
    for i, fr in enumerate(game_page.frames):
        if fr == game_page.main_frame:
            continue
        print(f"\n=== FRAME {i}: {fr.url[:120]} ===")
        dump_target(fr, f"frame{i}")

    shot = f"{SHOTS}/mines-probe-{int(time.time())}.png"
    try:
        game_page.screenshot(path=shot, full_page=True)
        print(f"\n    screenshot: {shot}")
    except Exception as e:
        print(f"    screenshot failed: {str(e)[:100]}")

    # --- watch it --------------------------------------------------------
    # Nothing is clicked. This just records whether anything changes on its
    # own, which is how the "Auto cashout in: N" countdown and the round
    # state will be recognised later.
    print(f"\n--- watching for {args.secs}s (no clicks) ---")
    seen = {}
    deadline = time.time() + args.secs
    while time.time() < deadline:
        try:
            txt = (game_page.inner_text("body") or "")
        except Exception:
            txt = ""
        for line in {l.strip() for l in txt.splitlines() if l.strip()}:
            seen[line[:48]] = seen.get(line[:48], 0) + 1
        game_page.wait_for_timeout(2000)
    print("    text seen while idle (count out of ~"
          f"{args.secs // 2} samples):")
    for line, n in sorted(seen.items(), key=lambda kv: -kv[1])[:30]:
        print(f"      {n:>3}x  {line}")

    print("\n--- done. No bet was placed, nothing in the game was clicked. ---")
finally:
    for closer in (lambda: context and context.close(),
                   lambda: browser and browser.close(),
                   lambda: pw and pw.stop(),
                   lambda: bridge and m.stop_bridge(bridge)):
        try:
            closer()
        except Exception:
            pass
