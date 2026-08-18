"""Find out how a baccarat table signals its betting window, read-only.

Why this exists: the khelofun tournament of 2026-08-17 ran 150 hands and
staked nothing. Every hand timed out in tournament.wait_for_window_open()
with `timer=None` -- [data-role="circle-timer"], the element baccarat's
main._betting_open() gates on, was never present. The chip rail check passed
on the same frame, so the seat was on the real game frame; only the phase
signal was missing.

circle-timer was verified on cricmatch247 (2026-07-17). This samples a live
table on whichever site you point it at and prints what actually changes
between the open and closed phases, so the detector can be written from a
dump instead of a guess -- same precedent as inspect_form.py.

Seats through tournament.Seat, so it walks the exact path the tournament
does (login -> lobby -> Baccarat A -> game frame), including proxies.

Places no bets. It only reads the DOM.

Usage:
  .venv/bin/python probe_baccarat_window.py <username> <password> \
      [--env .env.tournament.khelofun] [--secs 150] [--interval 0.5]
"""
import argparse
import collections
import os
import sys
import time

# --env must beat main.py's own bare load_dotenv() at import time, same
# ordering gotcha as every other script here.
_env_file = None
if "--env" in sys.argv:
    _env_file = sys.argv[sys.argv.index("--env") + 1]

import main as m  # noqa: E402

if _env_file:
    from dotenv import load_dotenv
    load_dotenv(_env_file, override=True)

import tournament as t  # noqa: E402
from sites.games import BACCARAT  # noqa: E402

parser = argparse.ArgumentParser()
parser.add_argument("username")
parser.add_argument("password")
parser.add_argument("--env", default=None)
parser.add_argument("--url", default=None)
parser.add_argument("--proxy", default=None)
parser.add_argument("--secs", type=float, default=150)
parser.add_argument("--interval", type=float, default=0.5)
parser.add_argument("--seats", type=int, default=1,
                    help="seat this many accounts at once; pass a "
                         "comma-separated username list")
parser.add_argument("--spacing", type=float, default=20,
                    help="seconds between login starts (a burst "
                         "from one IP trips the 403 rate block)")
args = parser.parse_args()

SITE_URL = args.url or os.environ.get("BOT_SITE_URL") or m.SITE_URL

PROXY = args.proxy
if PROXY is None:
    raw = os.environ.get("TOURNAMENT_PROXIES", "").replace(",", " ").split()
    PROXY = raw[0] if raw else None


# One sample: everything that could plausibly mark the betting phase.
#
# `roles` is the set of data-role values that are actually VISIBLE (a hidden
# element is not a phase signal), so diffing samples shows which roles come
# and go with the window. `texts` catches a banner that lives in text rather
# than in element presence -- how Stock Market Live turned out to work.
_SAMPLE_JS = """() => {
    const vis = e => { const r = e.getBoundingClientRect();
                       return r.width > 0 && r.height > 0 && e.offsetParent !== null; };
    const g = r => document.querySelector(`[data-role="${r}"]`);
    const txt = e => e ? (e.innerText || '')
        .replace(/[\\u2066\\u2069\\u200b]/g, '').trim() : null;

    const roles = new Set();
    for (const e of Array.from(document.querySelectorAll('[data-role]'))) {
        if (vis(e)) roles.add(e.getAttribute('data-role'));
    }

    // Short visible strings -- a phase banner is always short.
    const texts = new Set();
    for (const e of Array.from(document.querySelectorAll('div,span,p'))) {
        if (!vis(e)) continue;
        const s = txt(e);
        if (s && s.length <= 30 && !s.includes('\\n')) texts.add(s.toUpperCase());
    }

    const timer = g('circle-timer');
    const chips = Array.from(document.querySelectorAll('[data-role="chip"]'))
        .map(e => { const r = e.getBoundingClientRect();
                    return {v: e.getAttribute('data-value'),
                            w: Math.round(r.width), h: Math.round(r.height),
                            cur: getComputedStyle(e).cursor}; });

    return {
        roles: Array.from(roles).sort(),
        texts: Array.from(texts).sort(),
        timer: timer ? {h: Math.round(timer.getBoundingClientRect().height),
                        vis: timer.offsetParent !== null} : null,
        instruction: txt(g('instruction-message')),
        status: txt(g('status-text')),
        totalBet: txt(g('total-bet-label-value')),
        balance: txt(g('balance-label-value')),
        chips: chips,
        chipCount: chips.length,
        clickableChips: chips.filter(c => c.cur === 'pointer').length,
        selectedChip: txt(g('selected-chip')),
    };
}"""


def _mask(proxy_str):
    """host:port:user:pass -> host:port:user:*** (never echo the password)."""
    if not proxy_str:
        return "direct"
    bits = proxy_str.split(":")
    return ":".join(bits[:3] + ["***"]) if len(bits) >= 4 else proxy_str


def sample(frame):
    try:
        return frame.evaluate(_SAMPLE_JS)
    except Exception as exc:
        return {"error": str(exc)[:120]}


def multi(usernames, password, proxies, secs, interval, spacing):
    """Seat several accounts AT ONCE and report whether each one ever sees a
    betting window.

    This is the condition the tournament actually runs in -- the 2026-08-17
    khelofun run held ten live tables open simultaneously -- and a single
    seat cannot reproduce it. Per-seat timer counts tell a table-wide problem
    (nobody sees a window) apart from a load/concurrency one (the first few
    seats see windows and later ones do not)."""
    print(f"seating {len(usernames)} account(s) concurrently, "
          f"{spacing}s apart -- read-only, no bets\n")
    seats = [t.Seat(u, password, site_url=SITE_URL,
                    proxy=proxies[i % len(proxies)] if proxies else None)
             for i, u in enumerate(usernames)]
    futs = []
    for s in seats:
        futs.append(s.open_async(progress=lambda m: print(f"   {m}")))
        if spacing:
            time.sleep(spacing)

    live = []
    for s, f in zip(seats, futs):
        try:
            f.result(timeout=600)
            live.append(s)
            print(f"   ✅ {s.username}: table_id={s.table_id} balance={s.balance}")
        except Exception as exc:
            print(f"   XX {s.username} did not seat: {str(exc)[:90]}")

    if not live:
        print("\nno seats came up")
        for s in seats:
            s.close()
        return 1

    print(f"\n{len(live)}/{len(seats)} seated. sampling {secs:.0f}s…\n")
    tally = {s.username: {"timer": 0, "n": 0, "err": 0} for s in live}
    deadline = time.time() + secs
    try:
        while time.time() < deadline:
            futs = [(s, s.call(sample, s.frame)) for s in live]
            for s, f in futs:
                try:
                    smp = f.result(timeout=30)
                except Exception:
                    smp = {"error": "call failed"}
                rec = tally[s.username]
                rec["n"] += 1
                if "error" in smp:
                    rec["err"] += 1
                elif smp.get("timer"):
                    rec["timer"] += 1
            time.sleep(interval)
    except KeyboardInterrupt:
        print("(interrupted)")
    finally:
        for s in seats:
            s.close()

    print("=" * 62)
    print(f"{'account':22s} {'samples':>8s} {'timer seen':>11s} {'errors':>8s}")
    print("-" * 62)
    for u, r in tally.items():
        print(f"{u:22s} {r['n']:8d} {r['timer']:11d} {r['err']:8d}")
    blind = [u for u, r in tally.items() if r["timer"] == 0]
    print()
    if not blind:
        print("every seat saw betting windows -- concurrency is NOT the cause")
    elif len(blind) == len(tally):
        print("NO seat ever saw a window -- table-wide, not a load problem")
    else:
        print(f"{len(blind)}/{len(tally)} seats never saw a window: {blind}")
        print("-> partial blindness, consistent with load/starvation")
    return 0


def main():
    if args.seats > 1:
        raw = os.environ.get("TOURNAMENT_PROXIES", "").replace(",", " ").split()
        proxies = [args.proxy] if args.proxy else (raw or [None])
        names = [u.strip() for u in args.username.split(",") if u.strip()]
        return multi(names, args.password, proxies, args.secs, args.interval,
                     args.spacing)

    print(f"site   : {SITE_URL}")
    print(f"account: {args.username}")
    print(f"proxy  : {_mask(PROXY)}")
    print(f"sampling {args.secs:.0f}s every {args.interval}s -- read-only, no bets\n")

    seat = t.Seat(args.username, args.password, site_url=SITE_URL, proxy=PROXY)
    try:
        seat.open_async(progress=lambda s: print(f"   {s}")).result(timeout=600)
    except Exception as exc:
        print(f"XX could not seat {args.username}: {exc}")
        seat.close()
        return 1

    print(f"\n✅ seated. table_id={seat.table_id} balance={seat.balance}\n")
    print("t(s)  timer      chips(click)  totalBet  instruction / status")
    print("-" * 78)

    samples = []
    deadline = time.time() + args.secs
    t0 = time.time()
    try:
        while time.time() < deadline:
            s = seat.call(sample, seat.frame).result(timeout=30)
            s["_t"] = round(time.time() - t0, 1)
            samples.append(s)
            if "error" in s:
                print(f"{s['_t']:5.1f}  ERROR {s['error']}")
            else:
                print(f"{s['_t']:5.1f}  {str(s['timer']):10s} "
                      f"{s['chipCount']:2d}({s['clickableChips']:2d})        "
                      f"{str(s['totalBet']):8s}  "
                      f"{s.get('instruction') or ''} {s.get('status') or ''}")
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\n(interrupted)")
    finally:
        seat.close()

    good = [s for s in samples if "error" not in s]
    if not good:
        print("\nno usable samples")
        return 1

    print("\n" + "=" * 78)
    print(f"{len(good)} samples over {good[-1]['_t']:.0f}s\n")

    # circle-timer: present at all?
    seen_timer = sum(1 for s in good if s["timer"])
    print(f"circle-timer present in {seen_timer}/{len(good)} samples"
          + ("  <-- baccarat's detector would work" if seen_timer else
             "  <-- NEVER present: this is why nothing was staked"))

    # Which roles are phase-dependent? Those are the window signal.
    counts = collections.Counter()
    for s in good:
        counts.update(s["roles"])
    always = [r for r, n in counts.items() if n == len(good)]
    sometimes = {r: n for r, n in counts.items() if 0 < n < len(good)}
    print(f"\nvisible data-roles: {len(counts)} distinct, "
          f"{len(always)} in every sample")
    if sometimes:
        print("ROLES THAT COME AND GO (candidate window signals):")
        for r, n in sorted(sometimes.items(), key=lambda kv: -kv[1]):
            print(f"   {r:42s} {n:4d}/{len(good)}")
    else:
        print("no role changes across phases -- the signal must be in TEXT")

    # Same for short visible texts -- how Stock Market Live signals its phase.
    tcounts = collections.Counter()
    for s in good:
        tcounts.update(s["texts"])
    tsometimes = {x: n for x, n in tcounts.items() if 0 < n < len(good)}
    print("\nTEXTS THAT COME AND GO (candidate phase banners):")
    for x, n in sorted(tsometimes.items(), key=lambda kv: -kv[1])[:30]:
        print(f"   {x[:44]:44s} {n:4d}/{len(good)}")

    # The chip rail is itself a window signal on baccarat: CLAUDE.md records
    # that between rounds only one chip node renders, non-interactive.
    rails = collections.Counter((s["chipCount"], s["clickableChips"])
                                for s in good)
    print("\nchip rail (count, clickable) -> samples:")
    for k, n in rails.most_common():
        print(f"   {k}  -> {n}")

    instr = collections.Counter((s.get("instruction") or "").upper()
                                for s in good)
    print("\ninstruction-message text -> samples:")
    for k, n in instr.most_common(10):
        print(f"   {k!r:44s} {n}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
