"""Per-GAME profile for the paired-hedge engine, the game-level counterpart to
SiteProfile in sites/base.py.

The hedge engine (main.run_paired_hedge) was written against Evolution
Baccarat, with "Banker"/"Player" and the baccarat bet-spot/timer markup baked
into it. A second hedgeable game (Evolution Stock Market Live, UP vs DOWN)
turned out to differ in every one of those details while reusing the same
login/lobby/frame machinery -- so everything game-specific moved here and the
engine reads it instead.

Both profiles below were captured live (2026-07-20) with the read-only probes
in probe_evo_lobby.py / probe_stock_round.py. Do not guess at a new game's
values: run the probes, read the dump, then fill one of these in.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class GameProfile:
    """Everything the hedge engine needs to drive one live-casino game."""

    key: str                  # short id, e.g. "baccarat" / "stockmarket"

    # --- how to reach the table -----------------------------------------
    # Baccarat sits in cricmatch's own lobby, so category + tile are enough.
    # Stock Market is NOT in that catalogue at all (confirmed live: 206 tiles
    # across Game Shows / Arcade Games / All contain no match, and the site
    # search returns only football teams for "Stock") -- it exists only inside
    # Evolution's own in-game lobby, which is reached by opening some other
    # Evolution game first and clicking its LOBBY button. `via_provider_lobby`
    # switches the engine to that longer route.
    category: str             # cricmatch lobby category tab, e.g. "Baccarat"
    tile_text: str            # game tile text, e.g. "Baccarat A"
    via_provider_lobby: bool = False
    lobby_search: str = ""    # what to type in Evolution's lobby search
    lobby_tile: str = ""      # the tile to click in Evolution's lobby
    # The LOBBY button in the running game's bottom-right corner, which opens
    # the provider's own lobby (a separate iframe -- see
    # main._find_provider_lobby_frame).
    lobby_button_role: str = "lobby-button"

    # --- the two sides of the hedge -------------------------------------
    # Full data-role values of the two opposing bet spots. NOTE the two games
    # use different naming conventions -- baccarat is "bet-spot-Banker",
    # stock market is "SM_Up" -- so these are complete role names, not
    # suffixes to be interpolated into a "bet-spot-{}" template.
    side_a_role: str = ""     # account 1's side
    side_b_role: str = ""     # account 2's side
    side_a_label: str = ""    # display name, e.g. "Banker" / "UP"
    side_b_label: str = ""
    side_a_icon: str = "🔴"
    side_b_icon: str = "🔵"

    # data-role proving the table is live and interactive (replaces the
    # hardcoded bet-spot-Banker probe in wait_for_live_table).
    table_ready_role: str = ""

    # --- betting-window detection ---------------------------------------
    # Baccarat exposes [data-role="circle-timer"] only while betting is open,
    # which is a clean boolean. Stock Market has no such element -- confirmed
    # live, its visible role SET is identical in every phase -- so the phase
    # must be read from the TEXT of an instruction banner instead.
    # Exactly one of these two is used, per `window_mode`.
    window_mode: str = "timer"          # "timer" | "instruction"
    timer_role: str = "circle-timer"
    instruction_role: str = "instruction-message"
    # Substrings (upper-cased comparison) marking the open-for-bets phase.
    instruction_open: tuple = ()

    # --- cash-out (stock market only) -----------------------------------
    needs_cashout: bool = False
    cashout_role: str = "cash-out"
    portfolio_role: str = "portfolio"
    # Fraction by which (portfolio_a + portfolio_b) may deviate from the total
    # staked before the run is stopped as a broken hedge. The two cash-outs
    # fire concurrently, but the chart moves continuously, so a gap between
    # them is real money -- this catches it after the fact.
    cashout_tolerance: float = 0.05

    # --- timing ----------------------------------------------------------
    # Sized to each game's own cycle. Baccarat's is ~45-60s; Stock Market's is
    # faster and has a long post-bet "decision" phase instead of a settle.
    drain_secs: int = 30      # let a mid-way window pass before betting
    place_secs: int = 150     # how long to hunt for a clean both-open window
    settle_secs: int = 40     # wait for the hand to resolve after betting


# Evolution Baccarat -- reproduces the engine's original hardcoded behavior
# exactly, so existing /run history and the gameplay bot are unaffected.
BACCARAT = GameProfile(
    key="baccarat",
    category="Baccarat",
    tile_text="Baccarat A",
    side_a_role="bet-spot-Banker",
    side_b_role="bet-spot-Player",
    side_a_label="Banker",
    side_b_label="Player",
    table_ready_role="bet-spot-Banker",
    window_mode="timer",
)

# Evolution Stock Market Live. Roles captured live 2026-07-20 -- SM_Up/SM_Down
# are the bet spots, `cash-out` and `portfolio` drive the cash-out step, and
# balance-label-value / total-bet-label-value are shared with baccarat (both
# read_game_balance and _read_total_bet were verified working unchanged here).
# Table minimum is 10 rupees, vs baccarat's 100 -- so live testing is 10x
# cheaper on this game.
STOCKMARKET = GameProfile(
    key="stockmarket",
    category="Baccarat",          # the door in: any Evolution game will do
    tile_text="Baccarat A",
    via_provider_lobby=True,
    lobby_search="Stock",
    lobby_tile="Stock Market",
    side_a_role="SM_Up",
    side_b_role="SM_Down",
    side_a_label="UP",
    side_b_label="DOWN",
    side_a_icon="🟢",
    side_b_icon="🔴",
    table_ready_role="SM_Up",
    window_mode="instruction",
    instruction_open=("PLACE YOUR BETS",),
    # ON, but note this game hedges fine WITHOUT it -- established live
    # 2026-07-20 over four real ₹10/side rounds, where the combined balance
    # across both accounts went 3749 -> 3748 -> 3748 -> 3749. Money moved
    # between the accounts each round (±₹4-9) and the pair netted ~zero,
    # because both sides hold equal, opposite positions on one round. Settling
    # is also cheaper (the 1% fee is charged on cash-out) and has no timing
    # risk. So if cash-out ever regresses, turning this False is a safe,
    # already-proven fallback rather than a degradation.
    #
    # Runs 3 and 4 failed to cash out because the click went into a button
    # that was still greyed: PORTFOLIO already reads the stake once the chip
    # is staged, and stays that way through the gap between the betting window
    # closing and the position actually riding. The root [data-role="cash-out"]
    # reports disabled=false / opacity=1 / pointerEvents=auto in EVERY phase,
    # so none of the obvious properties distinguish the states -- the panel
    # greys itself by dropping the inner CASH OUT label to opacity 0.5, which
    # is what _cashout_enabled() now reads.
    needs_cashout=True,
    # Cycle measured live 2026-07-20 on a real table: the "PLACE YOUR BETS"
    # banner counts 10 -> 2 over roughly TEN seconds, then the phase becomes
    # "NEXT GAME SOON", and the next betting window opens ~85s after the
    # previous one. That window is noticeably tighter than baccarat's ~15s, and
    # a missed window costs a full 85s, so place_secs must span at least two
    # windows or a single unlucky miss ends the run.
    drain_secs=20,
    place_secs=220,
    settle_secs=90,
)

GAMES = {g.key: g for g in (BACCARAT, STOCKMARKET)}


def game_for(key):
    """Look up a GameProfile by key, defaulting to baccarat so existing
    callers that pass nothing keep their original behavior."""
    return GAMES.get((key or "").strip().lower(), BACCARAT)
