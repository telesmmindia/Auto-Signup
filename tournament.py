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
# 8 is roughly 3 chip selections + 8 spot clicks. Measured at ~2.3s per seat
# against a table that drops fast repeat clicks (see CLICK_SPACING_SECS below),
# which leaves real margin in a ~15s window. Raise it only after a live run
# shows that margin holds with every seat clicking at once on one machine.
# (The floor is the ~100 sub-chip remainder, which no click budget fixes.)
MAX_BET_CLICKS = 8

# Each chip click is confirmed against TOTAL BET before the next is sent.
# Firing them blind does not work: live on 2026-08-10 a 400 stake (four clicks
# of the 100 chip) landed as 100 on one side and 200 on the other. Repeated
# clicks at the same coordinates arrive as double-clicks, which the game drops.
CLICK_SPACING_SECS = 0.3      # minimum gap between two clicks on one spot
CLICK_CONFIRM_SECS = 1.6      # how long to wait for one chip to show up
CLICK_POLL_SECS = 0.15
CLICK_RETRIES = 3             # consecutive dropped clicks before giving up

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

# A hand that settles nothing -- a tie, a betting window that never opened, a
# stake that would not go down -- is REPLAYED, never treated as an elimination.
# Waiting first is the point: every one of those causes is transient (the table
# sitting between rounds, a dropped click, a frame that has not caught up), and
# retrying instantly tends to miss the very same window again.
RETRY_WAIT_SECS = 20

# Consecutive hands that knock NOBODY out before a group is called genuinely
# stuck. A baccarat tie is ~9.5% per hand, so a few in a row is ordinary luck;
# this many in a row means something real is wrong (a dead frame, two seats on
# different tables) and more waiting will not fix it.
MAX_STALLED_HANDS = 10

# Seating one account is a login + the casino lobby + a live-video table load,
# and any of the three fails transiently (the SPRIBE overlay, a frame that never
# renders, a rate-limited login). Each retry builds a BRAND-NEW browser and logs
# in again, which is the only thing that clears a half-loaded table.
SEAT_ATTEMPTS = 3
SEAT_RETRY_WAIT_SECS = 30
# When the failure is the site's login RATE BLOCK rather than anything about
# the account, 30s is useless: the block runs ~20 minutes and holds regardless
# of pacing (CLAUDE.md, balance_checker findings). Wait a real interval instead.
SEAT_BLOCK_WAIT_SECS = 300

# Stages in a row that eliminate nobody before the whole tournament gives up.
# A stage replay re-seats every account in a fresh browser with a fresh login,
# so it fixes things an in-group replay cannot -- worth doing more than once
# before declaring the bracket unfinishable.
MAX_STALLED_STAGES = 3


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


# A frame this many consecutive polls in a row has stopped answering at all.
# The real windows on khelofun's Baccarat A cycle every ~40s (probed live
# 2026-08-14: open ~10-20s, then closed), so at WINDOW_POLL_SECS this is a few
# seconds of total silence -- far too short to be a normal closed phase.
DEAD_FRAME_POLLS = 25


def wait_for_window_open(frame, game=BACCARAT, wait_secs=WINDOW_WAIT_SECS):
    """Block until a FRESH betting window opens. Returns True, False or "dead".

    Waits for a closed->open edge rather than just "is it open now": joining a
    window already half elapsed leaves too little time to select chips and get
    every click down, and a stake that only half lands is an unhedged bet.

    "dead" means the frame stopped answering -- the tab is gone, the iframe was
    detached, or the provider dropped the session. This is NOT the same as a
    closed window and must not be reported as one. main._betting_open() answers
    False for both (it swallows its own exceptions), which cost a real run 52
    minutes on 2026-08-14: ten hands each waited the full four minutes and
    reported "no betting window opened in time" while the truth was that every
    seat had lost its table. Waiting cannot fix a dead frame -- only a fresh
    seat can -- so say so immediately instead of burning the deadline."""
    deadline = time.time() + wait_secs
    saw_closed = False
    silent = 0
    while time.time() < deadline:
        try:
            is_open = m._betting_open(frame, game)
            if _frame_gone(frame):
                silent += 1
            else:
                silent = 0
        except Exception:
            is_open, silent = False, silent + 1
        if silent >= DEAD_FRAME_POLLS:
            return "dead"
        if not is_open:
            saw_closed = True
        elif saw_closed:
            return True
        time.sleep(WINDOW_POLL_SECS)
    return False


def _frame_gone(frame):
    """True when `frame` is no longer a live, attached frame we can read.

    Checked every poll because _betting_open() cannot raise -- it catches its
    own errors and returns False, which is indistinguishable from a perfectly
    healthy table between rounds."""
    try:
        if frame.is_detached():
            return True
        return frame.page.is_closed()
    except Exception:
        return True


def place_stake(frame, role, plan, target=None, attempts=3, game=BACCARAT):
    """Click `plan`'s chips onto one bet spot. Returns the TOTAL BET after.

    One seat only ever bets one spot, so TOTAL BET is that seat's stake and
    can be compared directly against the planned amount.

    TOPS UP A SHORT STAKE while the window is still open, rather than clicking
    the plan once and hoping. Confirmed necessary by the first real run
    (2026-08-10): a 900 stake needing five clicks landed 600 on one side and
    500 on the other, because clicks were lost part-way through. That left the
    two sides betting DIFFERENT amounts on the same hand, which is not a hedge
    -- the pair ended up 70 ahead by luck, and would have been down by the same
    kind of margin had the other side won.

    So after clicking, this re-reads TOTAL BET and clicks the difference,
    up to `attempts` times, stopping early once the window closes (topping up
    after the window shuts does nothing, and would otherwise stack onto the
    NEXT round)."""
    target = int(target if target is not None else sum(plan))

    def total():
        return m._read_total_bet(frame) or 0

    last_click = [0.0]

    for attempt in range(max(1, attempts)):
        placed = total()
        short = target - placed
        if short <= 0:
            break
        # First pass clicks the whole plan; later passes only the shortfall.
        todo = plan if attempt == 0 and not placed else plan_stake(short)[1]
        if not todo:
            break

        for chip, count in group_plan(todo):
            if not _pick_chip(frame, chip):
                break
            landed = 0
            misses = 0
            while landed < count and misses < CLICK_RETRIES:
                before = total()
                # Space the clicks so the game does not see a double-click in
                # the first place. Waiting to DETECT a dropped click instead
                # costs a full CLICK_CONFIRM_SECS each time, which pushed an
                # 8-chip stake to ~13s against a ~15s window; spacing up front
                # keeps the same stake around 4s.
                gap = CLICK_SPACING_SECS - (time.time() - last_click[0])
                if gap > 0:
                    time.sleep(gap)
                try:
                    m._click_bet_spot(frame, role)
                except Exception:
                    pass
                last_click[0] = time.time()
                # Confirm THIS click moved the counter before firing the next.
                # Clicking blind is what failed live on 2026-08-10: four rapid
                # identical clicks for a 400 stake landed as one on one side and
                # two on the other. Repeated clicks at the same coordinates
                # arrive as double-clicks and the game drops them, so each chip
                # has to be seen to register before the next is sent.
                moved = False
                deadline = time.time() + CLICK_CONFIRM_SECS
                while time.time() < deadline:
                    time.sleep(CLICK_POLL_SECS)
                    if total() > before:
                        moved = True
                        break
                if moved:
                    landed += 1
                    misses = 0
                else:
                    misses += 1
                try:
                    if not m._betting_open(frame, game):
                        return total()
                except Exception:
                    return total()

        try:
            if not m._betting_open(frame, game):
                break
        except Exception:
            break
    return total()


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
    if not all(o is True for o in opened):
        # Name the real cause. A seat whose frame died needs a NEW seat, which
        # only a stage replay can give it -- replaying the hand against the
        # same dead frame just waits out the deadline again (52 minutes of
        # exactly that on 2026-08-14).
        dead = [s.username for s, o in zip(seats, opened) if o == "dead"]
        if dead:
            detail = ("lost the connection to the live table ("
                      + ", ".join(dead) + ") — no money was staked; "
                      "these seats need reopening, waiting cannot fix them")
        else:
            detail = "no betting window opened in time — no money was staked"
        for L in live:
            results.append({"pair": (L["a"].username, L["b"].username),
                            "stake": L["stake"], "winner": None, "loser": None,
                            "status": "table_lost" if dead else "not_placed",
                            "dead_seats": dead,
                            "message": detail})
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
    # Classify by WHAT IS ACTUALLY ON THE TABLE, not by whether it matches what
    # was asked for. The first real run (2026-08-10) got this wrong in the
    # dangerous direction: TOTAL BET read 600 and 500 against a wanted 900, and
    # because neither equalled 900 the old check reported "no money at risk"
    # and moved on. Both bets were real. The hand ran, Banker won, and the two
    # accounts moved -500 and +570 while the run recorded neither. Zero is the
    # only reading that means nothing was staked.
    staked = []
    for L in live:
        a, b, stake = L["a"], L["b"], L["stake"]
        tb_a = L["tb_a"] or 0
        tb_b = L["tb_b"] or 0

        if tb_a == stake and tb_b == stake:
            staked.append(L)
            continue

        if tb_a == 0 and tb_b == 0:
            results.append({"pair": (a.username, b.username), "stake": stake,
                            "winner": None, "loser": None, "status": "not_placed",
                            "message": f"neither side's stake registered "
                                       f"(TOTAL BET 0/0, wanted {stake}) — "
                                       "nothing was bet"})
            continue

        # Something is down but it is not a matched pair of bets. Whatever the
        # shape, this hand is live money and must be followed to settlement --
        # returning here is what lost track of it last time.
        if tb_a and tb_b:
            detail = (f"both sides bet but for DIFFERENT amounts "
                      f"({a.username} ₹{tb_a:,} vs {b.username} ₹{tb_b:,}, "
                      f"wanted ₹{stake:,} each) — the ₹{abs(tb_a - tb_b):,} "
                      "difference is unhedged")
        else:
            one = a if tb_a else b
            amt = tb_a or tb_b
            detail = (f"ONLY {one.username} got a bet down (₹{amt:,}, wanted "
                      f"₹{stake:,} each) — that whole stake is one-sided")
        L["mismatch"] = detail
        staked.append(L)

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

        # A hand that went down mismatched is real, settled money, but the two
        # sides were not covering each other -- so the result says nothing
        # about who "should" advance, and both accounts leave the bracket with
        # their true post-settlement balances recorded.
        if L.get("mismatch"):
            results.append({"pair": (a.username, b.username), "stake": stake,
                            "winner": None, "loser": None, "status": "unhedged",
                            "message": (
                                f"⚠️ UNHEDGED HAND — {L['mismatch']}. It "
                                f"settled: {a.username} {L['pre_a']}→{post_a} "
                                f"({d_a:+}), {b.username} {L['pre_b']}→{post_b} "
                                f"({d_b:+}). Both accounts leave the bracket; "
                                "these balances are real, not estimates.")})
            continue

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
    (winner_seat_or_None, [result dicts, in play order], [seats still alive]).

    A GROUP ONLY ENDS WHEN ONE ACCOUNT HOLDS THE POT. The only thing that
    removes an account is being drained below the table minimum. Every other
    outcome -- a tie, a window that never opened, a stake that would not
    register, a balance that could not be read -- replays the pair after a
    short wait instead of dropping it.

    That distinction was worth the whole pot in a real run (2026-08-11): the
    last hand of a five-account group came back "not_placed -- no betting
    window opened in time, no money was staked", both finalists were dropped,
    and the group ended with no winner and 4,850 stranded across two live
    accounts. Nothing had gone wrong with the money; the code simply treated
    "nothing happened" the same as "something broke". If you are tempted to
    drop an account on a non-`ok` status again, that is the run to re-read.

    The third return value is the seats still holding money, so a group that
    stops early hands them back to the bracket rather than writing them off.

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
    stalled = 0
    stuck = None

    while len(alive) > 1:
        hand_no += 1
        if hand_no > MAX_GROUP_HANDS:
            stuck = (f"{MAX_GROUP_HANDS} hands played without one account "
                     "holding the pot")
            progress(f"   XX {stuck} -- stopping this group")
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
            return None, history, alive

        by_name = {s.username: s for s in alive}
        survivors = [bye] if bye else []
        knocked_out = 0
        replays = 0

        for res in results:
            win, lose = res.get("winner"), res.get("loser")

            if win is not None and lose is not None:
                # A settled hand: the stake moved. The loser is only out once
                # it genuinely cannot cover the table minimum -- losing a hand
                # is not elimination (see the docstring above).
                survivors.append(win)
                if (lose.balance or 0) >= table_min:
                    survivors.append(lose)
                    progress(f"   OK {res['message']} "
                             f"({lose.username} still in on {lose.balance})")
                else:
                    knocked_out += 1
                    progress(f"   OK {res['message']} "
                             f"-- {lose.username} is out ({lose.balance})")
                continue

            # tie / not_placed / unhedged / error -- nothing was decided, so
            # both accounts stay in and the pair is played again. Balances are
            # re-read from the live table at the top of every hand, so even an
            # unhedged or unreadable one self-corrects on the replay.
            survivors.extend(by_name[u] for u in res["pair"] if u in by_name)
            replays += 1
            progress(f"   .. {res['message']} -- replaying this pair")

        if not survivors:
            # Only reachable if a hand returned no results at all.
            stuck = "no accounts came back from that hand"
            progress(f"   XX {stuck}")
            break

        alive = survivors

        # A dead table is the one failure replaying cannot mend: the seat needs
        # a fresh browser and a fresh login, which only a stage replay gives it.
        # End the group now so that happens in minutes instead of after ten
        # futile replays. Nobody is eliminated -- run_tournament carries these
        # seats forward with their real balances.
        lost = sorted({u for res in results for u in (res.get("dead_seats") or [])})
        if lost:
            stuck = ("lost the live table for " + ", ".join(lost)
                     + " -- these seats have to be reopened, which replaying "
                       "the hand cannot do")
            progress(f"   XX {stuck}")
            break

        if knocked_out:
            stalled = 0
        else:
            stalled += 1
            if stalled >= MAX_STALLED_HANDS:
                stuck = (f"{stalled} hands in a row knocked nobody out -- "
                         "the group is stuck on something real, not unlucky")
                progress(f"   XX {stuck}")
                break
            progress(f"   .. no eliminations this hand "
                     f"({stalled}/{MAX_STALLED_HANDS} before giving up)")

        if replays and len(alive) > 1:
            progress(f"   .. waiting {RETRY_WAIT_SECS}s before replaying")
            time.sleep(RETRY_WAIT_SECS)

    if len(alive) == 1 and not stuck:
        return alive[0], history, alive
    # Nobody holds the pot. Do NOT name the first survivor as winner just to
    # have one -- that would report a winner who does not actually hold the
    # money. The caller carries these seats (with their real balances) into the
    # next stage instead.
    return None, history, alive


def chunk(items, size):
    return [items[i:i + size] for i in range(0, len(items), size)]


def seat_accounts(group, gi, site_url, proxies, login_spacing=0, progress=None,
                  attempts=SEAT_ATTEMPTS):
    """Open a browser + live table for every account in `group`, retrying the
    ones that fail.

    Returns (ready_seats, failures), failures being
    [{"username", "password", "error"}, ...] for accounts that never seated.

    A retry throws the old browser away and builds a fresh one rather than
    re-driving the existing page: the observed failure ("could not open the
    'Baccarat A' table") leaves a half-loaded frame that nothing short of a new
    context recovers. Retries also land on the NEXT proxy in the rotation,
    since a rate-limited exit IP is one of the ways this fails."""
    progress = progress or (lambda _s: None)
    ready = []
    pending = list(group)
    last_error = {}
    seen = {}          # username -> (state, balance, detail) from diagnose

    for attempt in range(1, max(1, attempts) + 1):
        if not pending:
            break

        seats = [Seat(a["username"], a["password"], site_url=site_url,
                      proxy=proxies[(i + gi + attempt) % len(proxies)])
                 for i, a in enumerate(pending)]

        # Open concurrently, but space the LOGIN starts: a burst of ~10
        # simultaneous logins from one IP is exactly what trips the site's
        # bare-403 rate block.
        futs = []
        for s in seats:
            futs.append(s.open_async(progress=lambda msg: progress(f"   {msg}")))
            if login_spacing:
                time.sleep(login_spacing)

        still = []
        for acct, s, f in zip(pending, seats, futs):
            try:
                f.result(timeout=600)
                ready.append(s)
            except Exception as exc:
                s.error = str(exc).splitlines()[0][:200]
                last_error[s.username] = s.error
                progress(f"   XX {s.username} could not be seated: {s.error}")
                # Free the browser/bridge before trying again, or a retried
                # group leaks one Chromium (and one pproxy) per failed attempt.
                s.close()
                still.append(acct)
        pending = still

        if not pending or attempt >= max(1, attempts):
            break

        # Before burning another round of REAL browser logins, ask the site
        # over HTTP what is actually wrong. A browser seat costs ~40s and a
        # login that counts against the very rate limit that may be causing
        # this; one HTTP login costs ~3s and says whether retrying can help.
        for a in pending:
            seen[a["username"]] = diagnose_account(
                a["username"], a["password"], site_url,
                proxies[gi % len(proxies)])
        kinds = {st for st, _, _ in seen.values() if st}

        if "blocked" in kinds:
            # Documented live: once tripped, the login block lasts ~20 minutes
            # and holds regardless of pacing, so a 30s retry is pure waste.
            progress(f"   .. the site is rate-limiting logins (the edge "
                     f"answered, not the app) -- waiting "
                     f"{SEAT_BLOCK_WAIT_SECS}s before trying again; retrying "
                     "sooner only extends the block")
            time.sleep(SEAT_BLOCK_WAIT_SECS)
        elif kinds == {"rejected"}:
            progress("   XX the site refused these credentials outright, so "
                     "another try cannot help -- not retrying them")
            break
        else:
            progress(f"   .. {len(pending)} seat(s) did not come up -- waiting "
                     f"{SEAT_RETRY_WAIT_SECS}s and rebuilding them "
                     f"(attempt {attempt + 1}/{attempts})")
            time.sleep(SEAT_RETRY_WAIT_SECS)

    failures = []
    for a in pending:
        state, bal, detail = seen.get(a["username"]) or diagnose_account(
            a["username"], a["password"], site_url,
            proxies[gi % len(proxies)])
        failures.append({"username": a["username"], "password": a["password"],
                         "error": last_error.get(a["username"], "unknown"),
                         "state": state, "balance": bal, "detail": detail})
    return ready, failures


def diagnose_account(username, password, site_url=None, proxy=None):
    """Ask the site directly what is wrong with an account, over HTTP.

    A browser seat costs ~40s and a real login; this costs ~3s and answers the
    question that decides everything else. Returns (state, balance, detail):

      "ok"       credentials work -- balance is the real figure. Whatever went
                 wrong was the table/browser path, so a retry can help.
      "blocked"  the edge/WAF answered, not the app (bare 403 / non-JSON, i.e.
                 http_check_account_balance's infra_block). Nothing about this
                 account was actually checked, and retrying faster only extends
                 the block -- see CLAUDE.md's login rate-limit findings.
      "rejected" the app answered and refused (wrong password, locked account).
                 A retry cannot help; the credentials need fixing.
      "unknown"  could not tell.

    The browser's own login timeout cannot tell "rejected" from "blocked" --
    it says "credentials rejected or the login was throttled" for both, which
    is exactly the ambiguity this resolves."""
    try:
        res = m.http_check_account_balance(username, password,
                                           site_url=site_url, proxy=proxy)
    except Exception as exc:
        return "unknown", None, str(exc).splitlines()[0][:200]
    detail = "; ".join(str(x) for x in (res.get("messages") or []))[:200]
    if res.get("ok"):
        return "ok", res.get("balance"), detail or "credentials fine"
    if res.get("infra_block"):
        return "blocked", None, detail or "edge/WAF block, the app never saw it"
    return "rejected", None, detail or "the site refused the login"


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
    stalled_stages = 0

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
            ready, failures = seat_accounts(
                group, gi, site_url, proxies, login_spacing=login_spacing,
                progress=progress)

            try:
                # An account that never seated must NOT just vanish from the
                # bracket -- that would silently leave its balance outside the
                # tournament while a winner is declared elsewhere. What to do
                # with it depends entirely on WHY it failed, which
                # diagnose_account() has already established over HTTP.
                blocked = []
                for f in failures:
                    state, bal = f["state"], f["balance"]

                    if state == "blocked":
                        # Nothing about this account was actually checked, so it
                        # is NOT eliminated and its money is NOT stranded -- the
                        # run simply could not reach the site. It goes back in
                        # the bracket and the next stage tries again.
                        progress(f"   XX {f['username']}: the site is rate-"
                                 "limiting logins, so it was never really "
                                 "checked -- it stays in the bracket")
                        summary["problems"].append(
                            {"account": f["username"], "stage": stage,
                             "problem": "login rate-limited (the edge blocked "
                                        "it, the site never saw it), so this "
                                        "account was never actually tried. "
                                        "Nothing is wrong with it; re-run once "
                                        "the block clears (~20 min)."})
                        blocked.append(f["username"])
                        continue

                    if state == "rejected":
                        progress(f"   XX {f['username']}: the site refused "
                                 f"these credentials ({f['detail']}) -- check "
                                 "the username/password")
                        summary["problems"].append(
                            {"account": f["username"], "stage": stage,
                             "problem": f"the site refused this login: "
                                        f"{f['detail']}. Credentials look "
                                        f"wrong; whatever it holds could not "
                                        f"be reached."})
                        note = "could not log in; credentials refused"
                    elif bal is not None and bal < table_min:
                        progress(f"   .. {f['username']} could not open a table "
                                 f"but holds {bal} (< table min {table_min}) -- "
                                 "already out, nothing stranded")
                        note = ("could not be seated; was already below the "
                                "table minimum, so nothing is stranded")
                    else:
                        held = "an unreadable balance" if bal is None else str(bal)
                        progress(f"   XX {f['username']} could not open a table "
                                 f"after {SEAT_ATTEMPTS} tries and holds {held} "
                                 "-- that money stays put")
                        summary["problems"].append(
                            {"account": f["username"], "stage": stage,
                             "balance": bal,
                             "problem": f"could not be seated after "
                                        f"{SEAT_ATTEMPTS} tries ({f['error']}); "
                                        f"holds {held} that the tournament "
                                        f"could not move"})
                        note = "could not be seated; balance left stranded"

                    if on_account:
                        on_account(f["username"], bal, "eliminated", stage)
                    summary["eliminated"].append(
                        {"account": f["username"], "stage": stage,
                         "balance": bal, "note": note})

                keep = {s.username for s in ready} | set(blocked)
                seated = [a for a in group if a["username"] in keep]

                if len(ready) < 2:
                    progress("   XX fewer than two seats came up -- skipping "
                             "this group, its seated accounts stay in the "
                             "bracket")
                    next_round.extend(seated)
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
                    next_round.extend(seated)
                    continue

                winner, history, still_alive = play_group(
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

                # A group that stopped without a winner has NOT eliminated the
                # accounts still holding money -- they carry into the next
                # stage with their real balances and keep playing. Writing them
                # off here is what ended a real run with the pot split across
                # two live accounts and "winner: null" (see play_group).
                advancing = [winner] if winner else [
                    s for s in still_alive if (s.balance or 0) >= table_min]
                if not winner and advancing:
                    summary["problems"].append(
                        {"stage": stage, "group": gi,
                         "problem": "group ended without a single winner; "
                                    + ", ".join(f"{s.username} holds "
                                                f"{s.balance}"
                                                for s in advancing)
                                    + " -- carried into the next stage"})

                for s in ready:
                    keeps_playing = s in advancing
                    result = ("winner" if (winner and s is winner)
                              else "playing on" if keeps_playing
                              else "eliminated")
                    if on_account:
                        on_account(s.username, s.balance, result, stage)
                    if not keeps_playing:
                        summary["eliminated"].append(
                            {"account": s.username, "stage": stage,
                             "balance": s.balance})

                for s in advancing:
                    row = dict(next(a for a in group
                                    if a["username"] == s.username))
                    row["balance"] = s.balance
                    next_round.append(row)

                # Rate-limited accounts were never actually tried, so they keep
                # their place in the bracket for the next stage to retry.
                for name in blocked:
                    next_round.append(dict(next(a for a in group
                                                if a["username"] == name)))

                if winner:
                    stage_rec["winners"].append(
                        {"account": winner.username, "balance": winner.balance})
                    progress(f"   ** group {gi} winner: {winner.username} "
                             f"(bal {winner.balance})")
                elif advancing:
                    progress(f"   .. group {gi} unresolved -- "
                             + ", ".join(f"{s.username} ({s.balance})"
                                         for s in advancing)
                             + " play on next stage")
            finally:
                # Seats that never came up were already closed inside
                # seat_accounts(); these are the ones that played.
                for s in ready:
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

        # A stage that eliminated nobody is retried rather than ending the
        # tournament: replaying a stage re-seats every account in a FRESH
        # browser with a fresh login, which is the strongest retry available
        # here and fixes the one thing play_group's in-group replays cannot
        # (a dead frame or a seat stuck on the wrong table). Only give up once
        # even that has changed nothing several times over.
        progressed = len(next_round) < len(contenders)
        contenders = next_round
        if progressed:
            stalled_stages = 0
            continue
        stalled_stages += 1
        if stalled_stages >= MAX_STALLED_STAGES:
            progress(f"\nXX {stalled_stages} stages in a row without a single "
                     "elimination -- stopping rather than looping forever")
            break
        progress(f"\n.. nobody was eliminated this stage -- re-seating every "
                 f"account and playing it again "
                 f"({stalled_stages}/{MAX_STALLED_STAGES})")

    if len(contenders) == 1 and not dry_run:
        summary["winner"] = contenders[0]["username"]
        summary["winner_balance"] = contenders[0].get("balance")
        progress(f"\n*** TOURNAMENT WINNER: {contenders[0]['username']} "
                 f"(bal {contenders[0].get('balance')}) ***")
    elif not dry_run:
        # No single account holds the pot. Say exactly who is still holding
        # what, so it can be finished by hand -- this is the state that
        # previously showed up only as a bare "winner": null.
        summary["unfinished"] = [
            {"account": c["username"], "balance": c.get("balance")}
            for c in contenders]
        held = ", ".join(
            f"{c['username']} ("
            + ("balance never read" if c.get("balance") is None
               else str(c["balance"])) + ")"
            for c in contenders)
        summary["problems"].append(
            {"problem": f"tournament ended with no single winner; still to be "
                        f"settled between {held}"})
        progress(f"\nXX NO WINNER -- still to be settled between {held}")
    summary["ended_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    save()
    return summary
