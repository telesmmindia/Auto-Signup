"""Read-only probe of Baccarat's chip rail and bet limits.

Why this exists: sites/games.py's BACCARAT has selectable_chips=False, on the
strength of an earlier probe that found only "hidden 0-value templates" for
[data-role="chip"]. Stock Market's rail, captured later, turned out to be
plain <div data-role="chip" data-value="10|50|...">. Evolution ships one UI
across its games, so the likely explanation is that the baccarat probe read
the DOM before the rail finished rendering -- the same too-early-read bug that
broke _open_via_provider_lobby (see CLAUDE.md). This re-checks by POLLING the
rail across several rounds instead of reading it once.

Also dumps the table's BET LIMITS (min/max per spot), which the knockout-
tournament work needs before any stake sizing can be written: the final rounds
of a 100-account bracket stake ~64x a starting balance, and if that exceeds the
table maximum the bracket cannot finish on one table.

Places NO bets. Clicks nothing inside the game beyond the navigation
_open_table_for already does.

Usage:
    .venv/bin/python probe_baccarat_chips.py <username> <password> \
        [--proxy host:port:user:pass] [--secs 150]
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
parser.add_argument("--secs", type=int, default=150,
                    help="how long to keep polling the rail (default 150s, "
                         "about three baccarat rounds)")
args = parser.parse_args()

SITE_URL = args.url or m.SITE_URL

# Every chip-ish element, with enough geometry/style to tell a real rendered
# chip from a hidden 0-value template -- the exact distinction the earlier
# probe got wrong.
_CHIP_DUMP_JS = """() => {
    const out = [];
    const nodes = document.querySelectorAll(
        '[data-role*="chip"], [data-value], [data-role*="bet-limits"], ' +
        '[data-role*="double"], [data-role*="undo"], [data-role*="repeat"]');
    for (const e of nodes) {
        const r = e.getBoundingClientRect();
        const cs = getComputedStyle(e);
        const attrs = {};
        for (const a of e.attributes) attrs[a.name] = a.value;
        out.push({
            role: attrs['data-role'] || null,
            value: attrs['data-value'] || null,
            tag: e.tagName,
            w: Math.round(r.width), h: Math.round(r.height),
            visible: r.width > 0 && r.height > 0 &&
                     cs.visibility !== 'hidden' && cs.display !== 'none',
            opacity: cs.opacity,
            cursor: cs.cursor,
            text: (e.innerText || '').trim().slice(0, 60),
        });
    }
    return out;
}"""

# The collapsed BET LIMITS / paytable tooltip keeps its text in the DOM even
# while hidden (established live -- it is why _TAG_BET_SPOT_JS has to exclude
# it), so the limits can be read without expanding anything.
_LIMITS_JS = """() => {
    const hits = [];
    const seen = new Set();
    for (const e of document.querySelectorAll('div,span,p,td,li')) {
        if (e.querySelector('div,span,p,td,li')) continue;   // leaf nodes only
        const t = (e.innerText || '').trim();
        if (!t || t.length > 200) continue;
        if (!/(BET LIMIT|MIN|MAX|LIMIT|₹|INR)/i.test(t)) continue;
        if (seen.has(t)) continue;
        seen.add(t);
        hits.push(t);
    }
    return hits.slice(0, 80);
}"""


def dump_chips(frame, label):
    """Print the chip-ish nodes, separating real rendered chips from templates."""
    try:
        nodes = frame.evaluate(_CHIP_DUMP_JS)
    except Exception as exc:
        print(f"    chip dump failed: {exc}")
        return []
    real = [c for c in nodes
            if c["visible"] and c["value"] not in (None, "", "0")]
    print(f"    {len(nodes)} chip-ish nodes, {len(real)} look REAL "
          f"(visible + non-zero value)")
    for c in real:
        print(f"      role={c['role']!r} value={c['value']!r} "
              f"{c['w']}x{c['h']} cursor={c['cursor']} text={c['text']!r}")
    if not real:
        # Show what IS there, so a negative result is diagnosable rather than
        # just "nothing found" like the original probe reported.
        for c in nodes[:12]:
            print(f"      (not real) role={c['role']!r} value={c['value']!r} "
                  f"visible={c['visible']} {c['w']}x{c['h']} "
                  f"opacity={c['opacity']} text={c['text']!r}")
    return real


pw, browser = m._launch_pw_browser()
context = None
bridge = None
game_page = None
try:
    proxy_conf = m.parse_proxy(args.proxy) if args.proxy else None
    if proxy_conf:
        proxy_conf, bridge = m.maybe_bridge_proxy(proxy_conf)

    print("--- opening Baccarat A through the production helper (read-only) ---")
    t0 = time.time()
    context, page, game_page, frame = m._open_table_for(
        browser, args.username, args.password, SITE_URL,
        BACCARAT.category, BACCARAT.tile_text,
        proxy_conf=proxy_conf, proxy=args.proxy, progress=lambda s: print(f"  {s}"),
        label=args.username, game=BACCARAT)
    print(f"--- table open in {time.time() - t0:.0f}s ---")

    print(f"table id : {m._table_id(game_page)!r}")
    print(f"balance  : {m.read_game_balance(frame)!r}")
    print(f"total bet: {m._read_total_bet(frame)!r}")

    print("\n--- BET LIMITS / currency text in the frame ---")
    try:
        limits = frame.evaluate(_LIMITS_JS)
        if limits:
            for t in limits:
                print(f"  {t!r}")
        else:
            print("  (nothing matched -- the limits panel may be canvas-drawn)")
    except Exception as exc:
        print(f"  limits dump failed: {exc}")

    print("\n--- polling the chip rail (the earlier probe read it once, too "
          "early) ---")
    first_real = None
    deadline = time.time() + args.secs
    while time.time() < deadline:
        elapsed = int(time.time() - t0)
        try:
            open_now = m._betting_open(frame, BACCARAT)
        except Exception as exc:
            open_now = f"error: {exc}"
        rail = m.read_chips(frame)
        print(f"\n  t+{elapsed}s betting_open={open_now} read_chips={rail!r}")
        dump_chips(frame, elapsed)
        if rail.get("chips") and first_real is None:
            first_real = (elapsed, rail)
            print(f"  *** RAIL FOUND at t+{elapsed}s: {rail!r} ***")
        time.sleep(10)

    print("\n=== VERDICT ===")
    if first_real:
        el, rail = first_real
        print(f"Baccarat DOES have a readable chip rail (first seen t+{el}s): "
              f"chips {rail['chips']}, selected {rail['selected']!r}.")
        print("=> selectable_chips can be flipped True for BACCARAT; "
              "read_chips()/select_chip() work here unchanged.")
    else:
        print("No usable chip rail appeared during the whole poll. The "
              "original selectable_chips=False finding holds, and staking an "
              "arbitrary amount on baccarat needs another mechanism (or the "
              "rail lives in a different frame than find_game_frame returns).")

finally:
    for closer in (lambda: game_page and game_page.close(),
                   lambda: context and context.close(),
                   lambda: browser.close(),
                   lambda: pw.stop(),
                   lambda: bridge and m.stop_bridge(bridge)):
        try:
            closer()
        except Exception:
            pass
