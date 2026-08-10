"""Knockout tournament engine: many accounts play down to a single winner.

Different shape from main.run_paired_hedge, which is why this is its own
module rather than another branch inside it. That one is TWO accounts running
a long loop of fixed-size bets whose whole point is that the pair's combined
balance does NOT move. This is MANY accounts, each pair staking its whole
balance on one hand, deliberately concentrating everything into one account
over ~7 rounds. Almost nothing about the round loop, the stake sizing, or the
failure handling carries across.

Game choice is not configurable, and the reason matters. A knockout needs the
loser's ENTIRE stake to move to the winner, and of the hedgeable two-sided
games only baccarat does that cheaply:

  baccarat      Player/Banker, full stake moves. Tie (~9.5%) returns both
                bets -- a dead round costs nothing but time. ~1.2% of the
                pot per round (5% commission on the ~46% of hands Banker
                wins). ~8% over a 7-round bracket.
  andar bahar   full stake moves, no tie, but ~2.6% per round.
  roulette      full stake moves, but a zero (2.7%) takes BOTH stakes --
                that destroys money rather than moving it, and a zero in the
                final round wipes out the account holding the whole pot.
  dragon tiger  no commission, but a tie (7.7%) takes HALF of both stakes,
                ~3.9% per round.
  stock market  UNUSABLE. Confirmed from real run history
                (pair_runs.stockmarket.json run #12, 100/side): the balances
                moved 96, then 11, then 36 -- payout is proportional to how
                far the chart travelled, so the loser keeps most of their
                stake and nobody is ever knocked out.

Chip rail facts, captured live 2026-07-XX by probe_baccarat_chips.py against
a real Baccarat A table -- these contradict sites/games.py's BACCARAT
(selectable_chips=False) and the CLAUDE.md note claiming baccarat's chip
nodes are "hidden 0-value templates":

  * The rail is REAL: [data-role="chip"] with data-value 100 / 500 / 2500 /
    10000 / 50000 / 100000.
  * It is only interactive DURING the betting window. Between rounds exactly
    one chip node renders, 32x32 with cursor:auto (a display, not a control);
    while betting is open there are six, 36x36 with cursor:pointer. The
    earlier probe read the table between rounds and concluded the rail was
    fake. It is not.
  * Consequence for this engine: chip selection must happen INSIDE the open
    window, alongside the bet clicks -- not before the round loop the way
    main.run_paired_hedge does it for Stock Market.
  * Table BET LIMITS read off the same panel: five ranges, the two main
    (100-minimum) spots capping at 500,000 and 200,000. Which row maps to
    Player/Banker was not established, so DEFAULT_TABLE_MAX below is the
    conservative one. Raise it only after reading the expanded panel.

Places real bets with real money. Every stake is verified against the game's
own TOTAL BET counter before the hand is allowed to run.
"""
from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import main as m
from chip_plan import group_plan, plan_stake as _plan_stake
from sites.games import BACCARAT

# Captured live off Baccarat A. Not read from the table at runtime because the
# rail is unreadable between rounds (see module docstring) and stake sizing has
# to happen before a window opens; verify_table_chips() below re-checks these
# against the live rail once a seat is open, and refuses to bet on a mismatch.
BACCARAT_CHIPS = (100, 500, 2500, 10000, 50000, 100000)

DEFAULT_TABLE_MIN = 100
# Conservative: the panel showed both 500,000 and 200,000 for the two main
# spots and we could not tell which is Player/Banker. Betting over the real cap
# is silently rejected by the table, which would land one side unhedged.
DEFAULT_TABLE_MAX = 200_000

# A stake is built by clicking chips one at a time inside a ~15s window, so the
# plan has to fit with room to spare -- a window that closes mid-stake leaves a
# half-sized bet down, which is a broken hedge.
#
# This is a direct trade against stranded money: whatever a plan cannot reach
# stays in the loser's account when they are knocked out. Measured with the
# exact solver in plan_stake (worst case across realistic stakes):
#
#     6 clicks -> up to 7.4% stranded
#     8 clicks -> up to 2.4%          <-- default
#    10 clicks -> up to 1.4%
#    16 clicks -> ~0%
#
# 8 is roughly 3 chip selections + 8 spot clicks, about 5s per seat. 10+ starts
# to crowd a 15s window once ten seats are clicking at once on one machine and
# competing for CPU. Raise it after a live run shows real timing headroom, not
# before. (The floor is the ~100 sub-chip remainder, which no budget fixes.)
MAX_BET_CLICKS = 8

WINDOW_POLL_SECS = 0.4
# How long to wait for a fresh betting window to OPEN. The live cycle measured
# ~35s (window open ~15s), so this spans several cycles.
WINDOW_WAIT_SECS = 240
# After betting closes, how long to wait for the hand to resolve and balances
# to update.
SETTLE_MAX_SECS = 150
SETTLE_POLL_SECS = 2

# Safety valve on a group that never resolves -- ties replay, and so does any
# hand whose loser still has money left (see play_group). Both are normal, but
# a group that has played this many hands without producing a winner is stuck
# on something real, not unlucky.
MAX_GROUP_HANDS = 60


# ---------------------------------------------------------------------------
# Stake sizing
# ---------------------------------------------------------------------------

def plan_stake(target, chips=BACCARAT_CHIPS, table_min=DEFAULT_TABLE_MIN,
               table_max=DEFAULT_TABLE_MAX, max_clicks=MAX_BET_CLICKS):
    """This module's defaults wrapped around the shared solver in chip_plan.py.

    The solver itself moved there so main.run_paired_hedge can build the same
    multi-chip stakes for the Stock Market hedge; tournament.py imports main,
    so main cannot import back. Behavior here is unchanged -- see chip_plan
    for the notes on why it is exact rather than greedy, and on the
    click-budget / stranded-money trade above."""
    return _plan_stake(target, chips, table_min=table_min,
                       table_max=table_max, max_clicks=max_clicks)


def verify_table_chips(frame, expected=BACCARAT_CHIPS, wait_secs=120):
    """Confirm the live rail really offers `expected`, before any money moves.

    The rail only populates during a betting window, so this polls rather than
    reading once -- the exact mistake that produced the wrong
    selectable_chips=False finding. Returns (ok, chips_seen, message)."""
    deadline = time.time() + wait_secs
    seen = []
    while time.time() < deadline:
        rail = m.read_chips(frame)
        seen = sorted({c for c in (rail.get("chips") or []) if c > 0})
        if seen:
            missing = [c for c in expected if c not in seen]
            if missing:
                return False, seen, (
                    f"table rail is {seen}, missing expected chips {missing} -- "
                    "stake sizing would be wrong, refusing to bet")
            return True, seen, f"rail confirmed: {seen}"
        time.sleep(1)
    return False, seen, ("chip rail never became readable within "
                         f"{wait_secs}s (no betting window seen?)")


def _pick_chip(frame, value, timeout_secs=5):
    """Select one chip denomination. Short-deadline variant of
    main.select_chip, which waits up to 75s by default -- far too long to sit
    inside a ~15s betting window."""
    deadline = time.time() + timeout_secs
    while time.time() < deadline:
        try:
            if m.read_chips(frame).get("selected") == value:
                return True
            loc = frame.locator(f'[data-role="chip"][data-value="{value}"]')
            if loc.count():
                loc.first.click(timeout=1500, force=True)
        except Exception:
            pass
        time.sleep(0.25)
    return m.read_chips(frame).get("selected") == value


def wait_for_window_open(frame, game=BACCARAT, wait_secs=WINDOW_WAIT_SECS):
    """Block until a FRESH betting window opens, and return True.

    Waits for a closed->open edge rather than just "is it open now": joining a
    window already half elapsed leaves too little time to select chips and get
    every click down, and a stake that only half lands is an unhedged bet."""
    deadline = time.time() + wait_secs
    saw_closed = False
    while time.time() < deadline:
        try:
            is_open = m._betting_open(frame, game)
        except Exception:
            is_open = False
        if not is_open:
            saw_closed = True
        elif saw_closed:
            return True
        time.sleep(WINDOW_POLL_SECS)
    return False


def place_stake(frame, role, plan):
    """Click `plan`'s chips onto one bet spot. Returns the TOTAL BET after.

    One seat only ever bets one spot, so TOTAL BET is that seat's stake and
    can be compared directly against the planned amount."""
    for chip, count in group_plan(plan):
        if not _pick_chip(frame, chip):
            break
        for _ in range(count):
            try:
                m._click_bet_spot(frame, role)
            except Exception:
                pass
    return m._read_total_bet(frame)


def wait_for_settle(frame, max_secs=SETTLE_MAX_SECS):
    """Wait for the hand to finish and the balance to settle.

    Uses the same signal main.run_paired_hedge does: TOTAL BET returning to 0
    means the round is over and the payout has been applied."""
    deadline = time.time() + max_secs
    while time.time() < deadline:
        try:
            if m._read_total_bet(frame) == 0:
                # The balance readout trails the counter slightly.
                time.sleep(2)
                return True
        except Exception:
            pass
        time.sleep(SETTLE_POLL_SECS)
    return False


# ---------------------------------------------------------------------------
# Seats: one account, one browser, one thread
# ---------------------------------------------------------------------------

@dataclass
class Seat:
    """One account parked at the live table, with its own browser and its own
    OS thread.

    The thread is not an optimisation -- Playwright's sync API requires every
    call for a given browser to happen on the thread that launched it (the
    same constraint main.run_paired_hedge works around with its per-side
    executor). Every Playwright touch below therefore goes through .call()."""

    username: str
    password: str
    site_url: str = None
    proxy: str = None

    _pw: object = field(default=None, repr=False)
    browser: object = field(default=None, repr=False)
    context: object = field(default=None, repr=False)
    page: object = field(default=None, repr=False)
    game_page: object = field(default=None, repr=False)
    frame: object = field(default=None, repr=False)
    bridge: object = field(default=None, repr=False)
    exec: ThreadPoolExecutor = field(default=None, repr=False)

    table_id: str = None
    balance: int = None
    error: str = None

    def call(self, fn, *args, **kwargs):
        """Submit a Playwright call to this seat's owning thread."""
        return self.exec.submit(fn, *args, **kwargs)

    # -- lifecycle ------------------------------------------------------
    def _open(self, progress):
        proxy_conf = m.parse_proxy(self.proxy) if self.proxy else None
        if proxy_conf:
            proxy_conf, self.bridge = m.maybe_bridge_proxy(proxy_conf)
        self._pw, self.browser = m._launch_pw_browser()
        self.context, self.page, self.game_page, self.frame = m._open_table_for(
            self.browser, self.username, self.password, self.site_url,
            BACCARAT.category, BACCARAT.tile_text,
            proxy_conf=proxy_conf, progress=progress, label=self.username,
            game=BACCARAT)
        self.table_id = m._table_id(self.game_page)
        self.balance = m.read_game_balance(self.frame)
        return self

    def open_async(self, progress=None):
        """Start login + table load on this seat's own thread. Returns a
        future -- the caller opens every seat concurrently and joins after."""
        self.exec = ThreadPoolExecutor(max_workers=1,
                                       thread_name_prefix=f"seat-{self.username}")
        progress = progress or (lambda _s: None)
        return self.exec.submit(self._open, progress)

    def _close(self):
        for closer in (lambda: self.game_page and self.game_page.close(),
                       lambda: self.context and self.context.close(),
                       lambda: self.browser and self.browser.close(),
                       lambda: self._pw and self._pw.stop()):
            try:
                closer()
            except Exception:
                pass

    def close(self):
        """Tear the seat down on its owning thread, then stop the proxy bridge.

        The bridge is a real subprocess -- skipping this leaks one pproxy per
        seat, and a 100-account tournament opens a lot of seats."""
        if self.exec:
            try:
                self.exec.submit(self._close).result(timeout=60)
            except Exception:
                pass
            self.exec.shutdown(wait=False)
            self.exec = None
        if self.bridge:
            try:
                m.stop_bridge(self.bridge)
            except Exception:
                pass
            self.bridge = None

    def refresh_balance(self):
        try:
            self.balance = self.call(m.read_game_balance, self.frame).result(timeout=30)
        except Exception:
            pass
        return self.balance


# ---------------------------------------------------------------------------
# One hand, many pairs
# ---------------------------------------------------------------------------
#
# Every seat in a group sits at the SAME table, so one hand resolves every
# pair in that group at once -- 5 pairs is 1 hand, not 5. That is the whole
# reason the tournament is quick despite each account needing a real login and
# a real live-video table load.
#
# It also means a hand cannot be called off once any bet is down. If one side
# of a pair fails to get its stake placed, the other side's money is already
# committed and the hand runs regardless. That pair is UNHEDGED for that hand
# -- real, one-sided exposure of a whole balance. It is reported loudly and
# both of its accounts are pulled out of the bracket rather than retried,
# because after a one-sided hand their balances no longer match the bracket's
# assumptions. main.run_paired_hedge retries this case; here it must not, since
# by the late rounds a single unhedged hand is most of the pot.
# ---------------------------------------------------------------------------

def _resolve(fut, timeout=60, default=None):
    try:
        return fut.result(timeout=timeout)
    except Exception:
        return default


def play_hand(pairs, table_min=DEFAULT_TABLE_MIN, table_max=DEFAULT_TABLE_MAX,
              progress=None, dry_run=False):
    """Play ONE hand covering every pair in `pairs` (a list of (seat_a, seat_b)).

    seat_a bets Banker, seat_b bets Player. Returns a list of per-pair result
    dicts: {"pair", "stake", "winner", "loser", "status", "message"}.
    status is one of: "ok", "tie", "walkover", "no_stake", "unhedged",
    "not_placed", "error".
    """
    progress = progress or (lambda _s: None)
    results = []
    live = []          # pairs that actually got money down

    # --- balances, concurrently across every seat ---------------------
    futs = [(p, p[0].call(m.read_game_balance, p[0].frame),
                p[1].call(m.read_game_balance, p[1].frame)) for p in pairs]
    for (a, b), fa, fb in futs:
        a.balance = _resolve(fa, 30, a.balance)
        b.balance = _resolve(fb, 30, b.balance)

    # --- same table? --------------------------------------------------
    for a, b in pairs:
        if a.table_id and b.table_id and a.table_id != b.table_id:
            results.append({"pair": (a.username, b.username), "stake": 0,
                            "winner": None, "loser": None, "status": "error",
                            "message": f"different tables ({a.table_id} vs "
                                       f"{b.table_id}) -- not the same hand"})
            continue
        if not (a.table_id and b.table_id):
            results.append({"pair": (a.username, b.username), "stake": 0,
                            "winner": None, "loser": None, "status": "error",
                            "message": "could not read both table ids -- "
                                       "refusing to bet rather than assume "
                                       "the same-table check passed"})
            continue

        # --- stake ----------------------------------------------------
        if a.balance is None or b.balance is None:
            results.append({"pair": (a.username, b.username), "stake": 0,
                            "winner": None, "loser": None, "status": "error",
                            "message": "could not read both balances"})
            continue
        stake, plan = plan_stake(min(a.balance, b.balance),
                                 table_min=table_min, table_max=table_max)
        if not stake:
            # One side is under the table minimum: it cannot bet at all, so
            # the richer account advances without a hand being played.
            rich, poor = (a, b) if (a.balance or 0) >= (b.balance or 0) else (b, a)
            results.append({"pair": (a.username, b.username), "stake": 0,
                            "winner": rich, "loser": poor, "status": "walkover",
                            "message": f"{poor.username} has "
                                       f"{poor.balance} (< table min "
                                       f"{table_min}) -- {rich.username} "
                                       "advances without a bet"})
            continue
        live.append({"a": a, "b": b, "stake": stake, "plan": plan,
                     "pre_a": a.balance, "pre_b": b.balance})

    if not live:
        return results

    for L in live:
        progress(f"   {L['a'].username} (Banker) vs {L['b'].username} "
                 f"(Player) — ₹{L['stake']:,} each "
                 f"[{'+'.join(str(c) for c in L['plan'])}]")

    if dry_run:
        for L in live:
            results.append({"pair": (L["a"].username, L["b"].username),
                            "stake": L["stake"], "winner": None, "loser": None,
                            "status": "dry_run",
                            "message": f"would stake ₹{L['stake']:,} each"})
        return results

    # --- wait for a fresh window, on every seat at once ---------------
    progress("   ⏳ waiting for a fresh betting window…")
    seats = [s for L in live for s in (L["a"], L["b"])]
    wfuts = [s.call(wait_for_window_open, s.frame) for s in seats]
    opened = [_resolve(f, WINDOW_WAIT_SECS + 30, False) for f in wfuts]
    if not all(opened):
        for L in live:
            results.append({"pair": (L["a"].username, L["b"].username),
                            "stake": L["stake"], "winner": None, "loser": None,
                            "status": "not_placed",
                            "message": "no betting window opened in time — "
                                       "no money was staked"})
        return results

    # --- place every stake concurrently -------------------------------
    progress(f"   🎲 placing {len(live)} pair(s) of bets…")
    pfuts = []
    for L in live:
        fa = L["a"].call(place_stake, L["a"].frame, BACCARAT.side_a_role, L["plan"])
        fb = L["b"].call(place_stake, L["b"].frame, BACCARAT.side_b_role, L["plan"])
        pfuts.append((L, fa, fb))
    for L, fa, fb in pfuts:
        L["tb_a"] = _resolve(fa, 60)
        L["tb_b"] = _resolve(fb, 60)

    # --- verify BEFORE the hand runs ----------------------------------
    staked = []
    for L in live:
        a, b, stake = L["a"], L["b"], L["stake"]
        ok_a, ok_b = L["tb_a"] == stake, L["tb_b"] == stake
        if ok_a and ok_b:
            staked.append(L)
            continue
        if not ok_a and not ok_b:
            results.append({"pair": (a.username, b.username), "stake": stake,
                            "winner": None, "loser": None, "status": "not_placed",
                            "message": f"neither side's stake registered "
                                       f"(TOTAL BET {L['tb_a']!r}/{L['tb_b']!r}, "
                                       f"wanted {stake}) — no money at risk"})
        else:
            exposed = a if ok_a else b
            other = b if ok_a else a
            results.append({"pair": (a.username, b.username), "stake": stake,
                            "winner": None, "loser": None, "status": "unhedged",
                            "message": (
                                f"⚠️ ONLY {exposed.username} got a bet down "
                                f"(TOTAL BET {L['tb_a']!r}/{L['tb_b']!r}, wanted "
                                f"{stake}). {other.username} did not. That is a "
                                f"one-sided ₹{stake:,} bet with nothing covering "
                                "it — this pair is out of the bracket, check "
                                "both balances by hand.")})

    if not staked:
        return results

    # --- let the hand run ---------------------------------------------
    progress("   ⏳ waiting for the hand to settle…")
    sfuts = [(L, L["a"].call(wait_for_settle, L["a"].frame)) for L in staked]
    for L, f in sfuts:
        _resolve(f, SETTLE_MAX_SECS + 30, False)

    # --- who won -------------------------------------------------------
    bfuts = [(L, L["a"].call(m.read_game_balance, L["a"].frame),
                 L["b"].call(m.read_game_balance, L["b"].frame)) for L in staked]
    for L, fa, fb in bfuts:
        a, b, stake = L["a"], L["b"], L["stake"]
        post_a = _resolve(fa, 30)
        post_b = _resolve(fb, 30)
        if post_a is None or post_b is None:
            results.append({"pair": (a.username, b.username), "stake": stake,
                            "winner": None, "loser": None, "status": "error",
                            "message": "hand ran but a balance could not be "
                                       "read afterwards — resolve by hand"})
            continue
        a.balance, b.balance = post_a, post_b
        d_a, d_b = post_a - L["pre_a"], post_b - L["pre_b"]

        # A tie returns both bets, so both balances come back to where they
        # started and nobody advances. Costs a hand, no money.
        if d_a == 0 and d_b == 0:
            results.append({"pair": (a.username, b.username), "stake": stake,
                            "winner": None, "loser": None, "status": "tie",
                            "message": "tie — both bets returned, replaying"})
            continue
        if d_a > 0 and d_b < 0:
            win, lose = a, b
        elif d_b > 0 and d_a < 0:
            win, lose = b, a
        else:
            results.append({"pair": (a.username, b.username), "stake": stake,
                            "winner": None, "loser": None, "status": "error",
                            "message": (f"balances moved unexpectedly "
                                        f"({L['pre_a']}→{post_a}, "
                                        f"{L['pre_b']}→{post_b}) — cannot tell "
                                        "who won, resolve by hand")})
            continue
        results.append({"pair": (a.username, b.username), "stake": stake,
                        "winner": win, "loser": lose, "status": "ok",
                        "message": (f"{win.username} won ₹{stake:,} "
                                    f"({lose.username} {L['pre_a'] if lose is a else L['pre_b']}"
                                    f"→{lose.balance})")})
    return results


# ---------------------------------------------------------------------------
# Groups and the bracket
# ---------------------------------------------------------------------------
#
# Why groups instead of one flat 100-account bracket: a flat bracket re-pairs
# every survivor globally each round, so with only ~10 browsers available every
# round means logging everyone back in -- ~200 logins for 100 accounts. The
# login endpoint hard-blocks (bare 403, ~20min) at roughly 20 logins in a few
# minutes from one IP; see CLAUDE.md's balance_checker findings. Playing a
# group of ~10 seats all the way down to ONE winner before closing any of them
# costs 10 logins instead, because the pairings stay inside the group. 100
# accounts becomes ~110 logins rather than ~200, and roughly 50 hands rather
# than 99.
#
# The end state is identical -- one account holding the pot -- because it is
# the same knockout, only the order of matchups differs.
# ---------------------------------------------------------------------------

def pair_up(entries):
    """Pair a list into [(a, b), ...] plus a bye when the count is odd.

    The bye goes to the SMALLEST balance. That account sits the round out and
    keeps what it has, which lets it catch up: since every stake is
    min(both balances), a short-stacked account otherwise drags its opponent's
    stake down and strands money in the winner."""
    ordered = sorted(entries, key=lambda s: (s.balance or 0), reverse=True)
    bye = ordered.pop() if len(ordered) % 2 else None
    return [(ordered[i], ordered[i + 1]) for i in range(0, len(ordered), 2)], bye


def play_group(seats, table_min=DEFAULT_TABLE_MIN, table_max=DEFAULT_TABLE_MAX,
               progress=None, dry_run=False, on_round=None):
    """Play an already-seated group down to a single winner.

    Every seat is at the same table, so each hand covers every pair in the
    group at once -- five pairs is one hand, not five. Returns
    (winner_seat_or_None, [result dicts, in play order]).

    KNOCKED OUT MEANS DRAINED, not "lost a hand". Losing a hand does not
    eliminate an account; dropping below the table minimum does. The
    difference is not cosmetic -- it was worth ~35% of the pot in simulation.

    Because a stake can only ever be min(both balances), a richer account that
    loses a hand keeps the difference. Eliminating it there stranded that money
    in a dead account: a mock 100-account run ended with the winner holding
    36.6% of the pot and 57.5% stranded across the losers, including one
    account knocked out of the final still holding 30,009. Replaying instead,
    until the loser genuinely cannot cover the table minimum, drains each loser
    to under ~100 and puts the pot where it belongs.

    Balances are re-read from the live table every hand (play_hand does it), so
    pairings re-sort by real balance as the group narrows."""
    progress = progress or (lambda _s: None)
    alive = list(seats)
    history = []
    hand_no = 0

    while len(alive) > 1:
        hand_no += 1
        if hand_no > MAX_GROUP_HANDS:
            progress(f"   XX {MAX_GROUP_HANDS} hands in this group without a "
                     "single winner -- stopping it")
            break

        pairs, bye = pair_up(alive)
        progress(f" -- group hand {hand_no}: {len(pairs)} pair(s)"
                 + (f", bye -> {bye.username}" if bye else ""))
        results = play_hand(pairs, table_min=table_min, table_max=table_max,
                            progress=progress, dry_run=dry_run)
        history.extend(results)
        if on_round:
            on_round(hand_no, results, bye)

        if dry_run:
            # Nothing was staked, so nothing can advance -- report the shape
            # of the bracket and stop rather than inventing winners.
            return None, history

        survivors = [bye] if bye else []
        for res in results:
            win, lose = res.get("winner"), res.get("loser")
            if res["status"] == "tie":
                # Both bets returned. Nobody moved; both play on.
                survivors.extend(s for s in alive if s.username in res["pair"])
                progress(f"   .. {res['message']}")
            elif win is not None and lose is not None:
                survivors.append(win)
                if (lose.balance or 0) >= table_min:
                    # Still has money to bet -- not out yet.
                    survivors.append(lose)
                    progress(f"   OK {res['message']} "
                             f"({lose.username} still in on {lose.balance})")
                else:
                    progress(f"   OK {res['message']} "
                             f"-- {lose.username} is out ({lose.balance})")
            else:
                # unhedged / not_placed / error: both accounts leave the
                # group. Their balances are no longer trustworthy inputs.
                progress(f"   XX {res['message']}")

        if not survivors:
            progress("   XX nobody survived this group")
            return None, history
        if len(survivors) == len(alive) and hand_no > 1:
            # Every pair tied or replayed. Harmless, but worth seeing.
            progress("   .. no eliminations this hand")
        alive = survivors

    return (alive[0] if alive else None), history


def chunk(items, size):
    return [items[i:i + size] for i in range(0, len(items), size)]


def run_tournament(roster, site_url=None, proxies=None, group_size=10,
                   table_min=DEFAULT_TABLE_MIN, table_max=DEFAULT_TABLE_MAX,
                   progress=None, dry_run=False, login_spacing=0,
                   state_path=None, on_account=None):
    """Run the whole knockout.

    `roster` is [{"username", "password"}, ...]. `group_size` is how many
    browsers run at once -- each entry needs its own Chromium, so this is a
    machine-capacity limit, not a tuning knob.

    Returns a summary dict, also written to `state_path` after every group so a
    crash mid-tournament still leaves a record of who held what."""
    progress = progress or (lambda _s: None)
    proxies = list(proxies or [None])

    summary = {
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "entrants": len(roster), "group_size": group_size,
        "dry_run": dry_run, "stages": [], "winner": None,
        "eliminated": [], "problems": [], "ended_at": None,
    }

    def save():
        if state_path:
            try:
                Path(state_path).write_text(
                    json.dumps(summary, indent=2, default=str))
            except Exception:
                pass

    def hand_record(h):
        rec = {k: v for k, v in h.items() if k not in ("winner", "loser")}
        rec["winner"] = h["winner"].username if h.get("winner") else None
        rec["loser"] = h["loser"].username if h.get("loser") else None
        rec["pair"] = list(h["pair"])
        return rec

    contenders = [dict(r) for r in roster]
    stage = 0

    while len(contenders) > 1:
        stage += 1
        groups = chunk(contenders, group_size)
        progress(f"\n=== STAGE {stage}: {len(contenders)} account(s) in "
                 f"{len(groups)} group(s) of up to {group_size} ===")
        stage_rec = {"stage": stage, "entrants": len(contenders),
                     "groups": [], "winners": []}
        next_round = []

        for gi, group in enumerate(groups, 1):
            if len(group) == 1:
                progress(f"\n-- group {gi}: only {group[0]['username']} -- "
                         "advances unplayed")
                next_round.append(group[0])
                stage_rec["groups"].append({"group": gi,
                                            "accounts": [group[0]["username"]],
                                            "winner": group[0]["username"],
                                            "note": "single entrant, walkover"})
                continue

            progress(f"\n-- group {gi}/{len(groups)}: "
                     + ", ".join(a["username"] for a in group))
            seats = [Seat(a["username"], a["password"], site_url=site_url,
                          proxy=proxies[(i + gi) % len(proxies)])
                     for i, a in enumerate(group)]

            try:
                # Open every seat concurrently, but space the LOGIN starts: a
                # burst of ~10 simultaneous logins from one IP is exactly what
                # trips the site's bare-403 rate block.
                futs = []
                for s in seats:
                    futs.append((s, s.open_async(
                        progress=lambda msg: progress(f"   {msg}"))))
                    if login_spacing:
                        time.sleep(login_spacing)

                ready = []
                for s, f in futs:
                    try:
                        f.result(timeout=600)
                        ready.append(s)
                    except Exception as exc:
                        s.error = str(exc).splitlines()[0][:200]
                        progress(f"   XX {s.username} could not be seated: "
                                 f"{s.error}")
                        summary["problems"].append(
                            {"account": s.username, "stage": stage,
                             "problem": f"seating failed: {s.error}"})

                if len(ready) < 2:
                    progress("   XX fewer than two seats came up -- skipping "
                             "this group, its accounts stay in the bracket")
                    next_round.extend(group)
                    continue

                # Stake sizing depends entirely on the rail. Check it against
                # a real seat before any money moves.
                ok, seen, msg = ready[0].call(
                    verify_table_chips, ready[0].frame).result(timeout=180)
                progress(f"   rail: {msg}")
                if not ok:
                    summary["problems"].append(
                        {"stage": stage, "group": gi,
                         "problem": f"chip rail check failed: {msg}"})
                    progress("   XX refusing to bet in this group")
                    next_round.extend(group)
                    continue

                winner, history = play_group(
                    ready, table_min=table_min, table_max=table_max,
                    progress=progress, dry_run=dry_run)

                stage_rec["groups"].append(
                    {"group": gi, "accounts": [s.username for s in ready],
                     "winner": winner.username if winner else None,
                     "hands": [hand_record(h) for h in history]})

                for h in history:
                    if h["status"] in ("unhedged", "error"):
                        summary["problems"].append(
                            {"stage": stage, "group": gi,
                             "pair": list(h["pair"]), "problem": h["message"]})

                for s in ready:
                    is_win = bool(winner) and s is winner
                    if on_account:
                        on_account(s.username, s.balance,
                                   "winner" if is_win else "eliminated", stage)
                    if not is_win:
                        summary["eliminated"].append(
                            {"account": s.username, "stage": stage,
                             "balance": s.balance})

                if winner:
                    row = dict(next(a for a in group
                                    if a["username"] == winner.username))
                    row["balance"] = winner.balance
                    next_round.append(row)
                    stage_rec["winners"].append(
                        {"account": winner.username, "balance": winner.balance})
                    progress(f"   ** group {gi} winner: {winner.username} "
                             f"(bal {winner.balance})")
            finally:
                for s in seats:
                    s.close()

            save()

        summary["stages"].append(stage_rec)
        save()

        if dry_run:
            progress("\n(dry run -- stopping after the first stage's shape)")
            break
        if not next_round:
            progress("\nXX no accounts survived this stage")
            break
        if len(next_round) >= len(contenders):
            progress("\nXX no progress made this stage -- stopping rather "
                     "than looping forever")
            break
        contenders = next_round

    if len(contenders) == 1 and not dry_run:
        summary["winner"] = contenders[0]["username"]
        summary["winner_balance"] = contenders[0].get("balance")
        progress(f"\n*** TOURNAMENT WINNER: {contenders[0]['username']} "
                 f"(bal {contenders[0].get('balance')}) ***")
    summary["ended_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    save()
    return summary
