"""Why does nobody get a betting window? Read-only, places no bets.

Seats N accounts straight from the tournament's own sheet (so no credentials
are typed or printed) and samples every live table once a second, reporting
per seat how often the betting window was actually open.

The per-seat SPREAD is the whole point, and it is what a single-seat probe can
never show. Reading it:

  * every seat similar and healthy -> this configuration works
  * nobody ever sees a window      -> table-wide: the site or this egress
  * the first-opened seats see far fewer than the last-opened ones
                                   -> SEAT DECAY, see below

Seat decay is what it found on starexch on 2026-08-22, and it is the cause of
the tournament's "no betting window opened in time". Measured, 10 seats on one
table, 15s apart, sampled ~3 minutes:

    seat opened  1st   2nd   3rd   4th   5th   6th   7th   8th   9th  10th
    windows seen  16    31    32    48    45    46    60    64    63    80
    video live    45    81    81   121   121   121   157   157   157   174

Perfectly monotonic in the order the seats were opened, and the video feed
decays in step with it. The oldest seat had been open about two minutes longer
than the youngest and saw a fifth as many windows. Extrapolated, a seat is
useless after roughly five to ten minutes -- while a tournament wants to hold
one for half an hour.

Ruled out by the flags below, each with a run of its own: tab visibility (every
seat reports "visible"), the video stream (--noautoplay changes nothing), an
idle session (--poke clicks a chip every 15s, no change), machine CPU/RAM (the
box was at 40% with 25 cores free), seat count (10 seats behave like 6), and
the proxies (the decay is monotonic in open order, while proxies are assigned
round-robin, so seat 1 and seat 6 share an IP and do NOT match).

--reload also answers a tempting question: reloading the game tab does NOT
repair a decayed seat. It loses the frame entirely, and doing it to five seats
at once knocked out the other five as well. Only a fresh seat helps.
"""
import argparse
import os
import sys
import time

_env_file = None
if "--env" in sys.argv:
    _env_file = sys.argv[sys.argv.index("--env") + 1]

import main as m  # noqa: E402

if _env_file:
    from dotenv import load_dotenv
    load_dotenv(_env_file, override=True)

import tournament as t  # noqa: E402
import tournament_runner as R  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--env", default=None)
ap.add_argument("--seats", type=int, default=6)
ap.add_argument("--secs", type=float, default=120)
ap.add_argument("--spacing", type=float, default=20)
ap.add_argument("--front", type=int, default=0,
                help="call bring_to_front() on the game tab of the first N "
                     "seats, every --front-every seconds")
ap.add_argument("--front-every", type=float, default=10)
ap.add_argument("--novideo", type=int, default=0,
                help="keep the live video paused on the first N seats -- the "
                     "engine reads DOM only and never needs the picture")
ap.add_argument("--reload-at", type=float, default=0,
                help="halfway trick: after this many seconds, reload the game "
                     "tab of the first --reload seats and compare the window "
                     "counts before and after")
ap.add_argument("--reload", type=int, default=0)
ap.add_argument("--noautoplay", action="store_true",
                help="launch Chromium with autoplay blocked, so the live "
                     "video never starts streaming -- the engine reads DOM "
                     "only and never needs the picture")
ap.add_argument("--poke", type=int, default=0,
                help="every --poke-every seconds, click a chip on the first N "
                     "seats. Selecting a chip denomination is NOT a bet -- no "
                     "money is staked -- it just keeps the session active")
ap.add_argument("--poke-every", type=float, default=15)
ap.add_argument("--direct", action="store_true",
                help="ignore the configured proxies and go out on this "
                     "machine's own IP")
args = ap.parse_args()

SITE_URL = os.environ.get("BOT_SITE_URL") or m.SITE_URL
if args.noautoplay:
    m._ANTI_THROTTLE_ARGS = list(m._ANTI_THROTTLE_ARGS) + [
        "--autoplay-policy=user-gesture-required"]
    print("launching with autoplay blocked (no live video stream)")
proxies = [None] if args.direct else R.current_proxies()

SAMPLE_JS = """() => {
    const g = r => document.querySelector(`[data-role="${r}"]`);
    const txt = e => e ? (e.innerText || '').trim() : null;
    const t = g('circle-timer');
    const chips = Array.from(document.querySelectorAll('[data-role="chip"]'))
        .map(e => { const r = e.getBoundingClientRect();
                    return {w: Math.round(r.width),
                            cur: getComputedStyle(e).cursor}; });
    const vids = Array.from(document.querySelectorAll('video')).map(v => ({
        ready: v.readyState, paused: v.paused,
        t: Math.round((v.currentTime || 0) * 10) / 10}));
    return {
        vis: document.visibilityState,
        timer: t ? Math.round(t.getBoundingClientRect().height) : null,
        chips: chips.length,
        liveChips: chips.filter(c => c.cur === 'pointer').length,
        bal: txt(g('balance-label-value')),
        totalBet: txt(g('total-bet-label-value')),
        msg: (txt(g('instruction-message')) || txt(g('status-text')) || '').slice(0, 40),
        roles: Array.from(document.querySelectorAll('[data-role]'))
                    .map(e => e.getAttribute('data-role')).length,
        video: vids,
    };
}"""


def sample(frame):
    try:
        return frame.evaluate(SAMPLE_JS)
    except Exception as exc:
        return {"error": str(exc)[:100]}


ws = R.open_worksheet()
roster = R.roster_from_sheet(ws)[:args.seats]
print(f"site      : {SITE_URL}")
print(f"seats     : {len(roster)}")
print(f"egress    : {'DIRECT (this machine IP)' if args.direct else str(len(proxies)) + ' proxy IP(s)'}")
print(f"sampling  : {args.secs:.0f}s at 1s, read-only, no bets\n")

seats = [t.Seat(r["username"], r["password"], site_url=SITE_URL,
                proxy=proxies[i % len(proxies)])
         for i, r in enumerate(roster)]
futs = []
for s in seats:
    futs.append(s.open_async(progress=lambda msg: None))
    if args.spacing:
        time.sleep(args.spacing)

live = []
for s, f in zip(seats, futs):
    try:
        f.result(timeout=600)
        live.append(s)
        print(f"   OK  {s.username:24s} table={s.table_id} bal={s.balance}")
    except Exception as exc:
        print(f"   XX  {s.username:24s} did not seat: {str(exc)[:80]}")

if not live:
    print("\nno seats came up")
    for s in seats:
        s.close()
    sys.exit(1)

print(f"\n{len(live)}/{len(seats)} seated, sampling…\n")
def _blank():
    return {"n": 0, "timer": 0, "live_rail": 0, "err": 0, "vid_moving": 0,
            "last": None, "first_timer": None}


tally = {s.username: _blank() for s in live}
before = {}


def reload_seat(seat):
    """Reload just the game tab -- no logout, no new login, so it costs
    nothing against the login rate limit."""
    seat.game_page.reload(wait_until="domcontentloaded", timeout=60000)
    fr = m.find_game_frame(seat.game_page, "evo-games.com", timeout_ms=60000)
    if fr:
        seat.frame = fr
    return fr is not None
t0 = time.time()
deadline = t0 + args.secs
prev_vid = {}
PAUSE_JS = """() => {
    let n = 0;
    for (const v of Array.from(document.querySelectorAll('video'))) {
        try { v.pause(); n++; } catch (e) {}
    }
    return n;
}"""
muted = set(s.username for s in live[:args.novideo])
if muted:
    print("keeping the video paused on: " + ", ".join(sorted(muted)) + "\n")
poked = set(s.username for s in live[:args.poke])
if poked:
    print("clicking a chip (no bet) every "
          f"{args.poke_every:.0f}s on: " + ", ".join(sorted(poked)) + "\n")
last_poke = 0.0


def poke(frame):
    """Select a chip denomination. This places NO money -- it only changes
    which chip a click would use -- but it is a real interaction with the
    provider's session."""
    try:
        return m.select_chip_fast(frame, 100, timeout_secs=3)
    except Exception:
        return False


fronted = set(s.username for s in live[:args.front])
if fronted:
    print(f"bring_to_front() every {args.front_every:.0f}s on: "
          + ", ".join(sorted(fronted)) + "\n")
last_front = 0.0
reloaded = False
while time.time() < deadline:
    if (args.reload_at and not reloaded
            and time.time() - t0 >= args.reload_at):
        reloaded = True
        for u, r in tally.items():
            before[u] = dict(r)
        targets = live[:args.reload]
        print(f"\n-- reloading the game tab of "
              + ", ".join(s.username for s in targets) + "\n")
        fs = [(s, s.call(reload_seat, s)) for s in targets]
        for s, f in fs:
            try:
                print(f"   {s.username}: reload ok={f.result(timeout=180)}")
            except Exception as exc:
                print(f"   {s.username}: reload failed: {str(exc)[:80]}")
        for u in tally:
            tally[u] = _blank()
        print()
    if poked and time.time() - last_poke >= args.poke_every:
        last_poke = time.time()
        for s in live:
            if s.username in poked:
                s.call(poke, s.frame)
    if muted:
        for s in live:
            if s.username in muted:
                s.call(lambda f=s.frame: f.evaluate(PAUSE_JS))
    if fronted and time.time() - last_front >= args.front_every:
        last_front = time.time()
        for s in live:
            if s.username in fronted:
                s.call(lambda p=s.game_page: p.bring_to_front())
    fs = [(s, s.call(sample, s.frame)) for s in live]
    for s, f in fs:
        try:
            smp = f.result(timeout=30)
        except Exception:
            smp = {"error": "call failed"}
        rec = tally[s.username]
        rec["n"] += 1
        rec["last"] = smp
        if "error" in smp:
            rec["err"] += 1
            continue
        if smp.get("timer"):
            rec["timer"] += 1
            if rec["first_timer"] is None:
                rec["first_timer"] = round(time.time() - t0)
        if smp.get("liveChips", 0) > 1:
            rec["live_rail"] += 1
        vt = [v.get("t") for v in (smp.get("video") or [])]
        if vt and prev_vid.get(s.username) != vt:
            rec["vid_moving"] += 1
        prev_vid[s.username] = vt
    time.sleep(1)

for s in seats:
    s.close()

print("=" * 88)
print(f"{'account':24s} {'front':>6s} {'samples':>7s} {'window open':>12s}"
      f" {'video moving':>13s} {'visibility':>11s} {'errors':>7s}")
print("-" * 88)
for u, r in tally.items():
    vis = (r["last"] or {}).get("vis", "?")
    b = before.get(u)
    tag = "yes" if u in (fronted | muted | poked) else "no"
    if b:
        pct_b = 100.0 * b["timer"] / max(1, b["n"])
        pct_a = 100.0 * r["timer"] / max(1, r["n"])
        print(f"{u:24s} {tag:>6s} {b['timer']:3d}/{b['n']:<3d} "
              f"({pct_b:4.0f}%) -> {r['timer']:3d}/{r['n']:<3d} ({pct_a:4.0f}%)"
              f"   video {r['vid_moving']:3d}/{r['n']:<3d}  {vis}")
    else:
        print(f"{u:24s} {tag:>6s} {r['n']:7d}"
              f" {r['timer']:12d} {r['vid_moving']:13d} {vis:>11s} {r['err']:7d}")
print()
for u, r in tally.items():
    print(f"{u}: last sample = {r['last']}")
blind = [u for u, r in tally.items() if r["timer"] == 0]
print()
if not blind:
    print("Every seat saw a betting window -- this configuration works.")
elif len(blind) == len(tally):
    print("NO seat ever saw a betting window. Not a per-seat problem: the "
          "table is not dealing to this machine/egress at all.")
else:
    print(f"{len(blind)}/{len(tally)} seats never saw a window: "
          + ", ".join(blind) + "\nThat is a CAPACITY limit, not a table fault "
          "-- the seats that did see windows prove the table is dealing.")
