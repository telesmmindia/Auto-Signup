"""Spribe Mines: the zero-reveal wagering engine.

WHAT IT DOES, in one line: stakes the whole bet, reveals NOTHING, and lets the
game's own idle "Auto cashout in: N" countdown end the round at 1.00x -- so the
stake comes straight back and the only thing produced is turnover.

WHY IT IS ITS OWN MODULE AND NOT A GameProfile
----------------------------------------------
Every game main.py drives so far is the EVOLUTION client on ezugi.evo-games.com,
and sites/games.py (GameProfile) is a description of THAT client: data-role bet
spots, a circle-timer, balance-label-value. Mines shares none of it. It is a
Spribe Angular app (app-version 2.1.50-hotfix, Angular 13) served in an iframe
at turbo.spribegaming.com/mines, and the single most important structural fact
is that **the 5x5 grid is a <canvas>** -- there are no tile elements at all.
That is a safety property here rather than a problem: the no-reveal rule this
whole approach rests on cannot be violated by a stray click, because there is
nothing in the DOM to click.

Everything below came out of the client's own saved markup, captured live on
2026-10-30 by probe_mines.py --save-html. Do not re-derive it from a screenshot.

    the mines count      div.number-of-mines button.dropdown-toggle   ("Mines: 3")
      its options        div.dropdown-list-number button.dropdown-item  (1..20)
    the stake            app-bet-spinner .input                       ("10.00")
      its numpad         app-bet-spinner app-numpad button            (digits, ".", backspace)
      its confirm        app-bet-spinner button.btn-done
      quick stakes       button.dropdown-toggle.all -> .fast-bets-menu .dropdown-item
    the BET button       button.btn-control.btn-bet
    the idle countdown   .auto-cashout-countdown     (display:none until a bet is live)
    the next multiplier  .next-tile                  ("Next: 1.10x")
    the lock overlay     .lock-ui                    (d-none while interactive)

The WALLET is NOT in this frame. The frame shows only the stake; the balance
lives in the operator's outer document (aviaport.spribegaming.com), which is
why read_wallet() takes the game PAGE and the rest take the FRAME.

The client refuses to render below 950x530 (app-screen-too-small), so the
context must be opened at a real desktop viewport -- a default headless window
is wide enough, but anything that shrinks it silently replaces the whole UI
with an apology.

⚠️ WHAT IS AND IS NOT CONFIRMED LIVE
------------------------------------
Confirmed, by running probe_mines.py against a real funded account: the login,
the WAF path, the launch URL, the host it lands on, the frame it lives in, and
every selector in the table above (they were read out of the saved DOM, not
guessed).

NOT confirmed: what the client looks like DURING a live round -- the BET
button's text and classes once staked, whether .lock-ui goes up, and above all
whether the idle countdown really ends the round at 1.00x and returns the
stake. That needs one real bet, which costs money, so it is deliberately left
to the operator:

    .venv/bin/python probe_mines.py <user> <pass> --url https://funcrash.com/ \
        --game-id 3139 --provider spribe --mines 1 --watch-round

Run that ONCE at the client's default stake before trusting this with a real
one. round_is_over() below is therefore written to accept SEVERAL independent
signals rather than one, so a wrong guess about any single one degrades into
"wait for the wallet to come back" instead of into a stuck round.
"""
import logging
import re
import time
from urllib.parse import urlsplit

import main as m

log = logging.getLogger(__name__)

TILE = "Mines"

# The idle countdown the recording shows is ~10s; a round therefore ends well
# inside this. The ceiling exists so a round that somehow never resolves gives
# up and reports it rather than hanging a worker thread forever.
ROUND_TIMEOUT_SECS = 120.0
# How long a BET click gets to visibly start a round before it counts as not
# taken. Found 2026-11-11 on funcrash: of 58 runs over one night, 40 ended
# with the client back at an idle, enabled BET, nothing live and the wallet
# unchanged -- i.e. the click never became a bet -- and they failed in bursts
# across every account and exit IP at once while playcric's games on the same
# machine kept betting. So it is the operator side refusing or stalling bets
# for a while, and the answer is to re-click after a pause, not to end the run.
BET_ACCEPT_SECS = 30.0
# Pauses before each re-click of a bet the game did not take. Their sum
# (~2.5 min) is how long an operator-side hiccup is ridden out before giving up.
BET_RETRY_WAITS = (10, 20, 40, 60)
# How long after a round resolves before the next bet. The client needs a beat
# to re-arm the BET button, and this is also the only pacing between bets.
ROUND_GAP_SECS = 2.0


class Walled(RuntimeError):
    """The game tab was WAF-walled and a token has been cached -- retry once."""


def _frame(game_page):
    """The Mines client's own frame. The launch tab's document is just the
    operator's header; everything that matters is one iframe down."""
    for f in game_page.frames:
        if f != game_page.main_frame and "/mines" in (f.url or ""):
            return f
    raise RuntimeError("the Mines game frame is not present in this tab")


def wait_for_client(game_page, timeout_secs=60):
    """Block until the Mines frame has actually rendered its controls.
    Returns the frame. The tab can be 'loaded' a long time before the Angular
    app inside it has drawn anything."""
    deadline = time.time() + timeout_secs
    last = ""
    while time.time() < deadline:
        try:
            fr = _frame(game_page)
            if fr.evaluate(
                    "() => !!document.querySelector('button.btn-control.btn-bet')"
                    " && !!document.querySelector('app-bet-spinner .input')"):
                return fr
        except Exception as e:
            last = str(e)[:80]
        game_page.wait_for_timeout(500)
    raise RuntimeError(f"the Mines client never finished loading"
                       f"{': ' + last if last else ''}")


# --- reading ---------------------------------------------------------------

_MONEY = re.compile(r"[0-9][0-9,]*\.[0-9]{2}")


def _money(text):
    """First money-shaped figure in `text`, as a float. None if there is none
    -- which means 'could not read', never 'zero'. Same rule as
    main.read_wallet_balance()."""
    if not text:
        return None
    hit = _MONEY.search(text)
    if not hit:
        return None
    try:
        return float(hit.group(0).replace(",", ""))
    except ValueError:
        return None


def read_wallet(game_page):
    """The operator's wallet, from the OUTER document. The game frame does not
    carry it. None means 'could not read' -- a caller must never treat that as
    a balance of zero."""
    try:
        return _money(game_page.inner_text("body"))
    except Exception:
        return None


def read_stake(frame):
    """What the bet input currently holds."""
    try:
        return _money(frame.inner_text("app-bet-spinner .input"))
    except Exception:
        return None


def read_mines_count(frame):
    try:
        txt = frame.inner_text("div.number-of-mines button.dropdown-toggle")
    except Exception:
        return None
    hit = re.search(r"(\d+)", txt or "")
    return int(hit.group(1)) if hit else None


_STATE_JS = """() => {
  const bet = document.querySelector("button.btn-control.btn-bet");
  const cd  = document.querySelector(".auto-cashout-countdown");
  const lock = document.querySelector(".lock-ui");
  const bar = document.querySelector(".progress-bar");
  return {
    bet_text: bet ? (bet.innerText || "").trim().replace(/\\s+/g, " ") : null,
    bet_disabled: bet ? !!bet.disabled : null,
    countdown_up: !!(cd && getComputedStyle(cd).display !== "none"),
    locked: lock ? !lock.classList.contains("d-none") : false,
    progress: bar ? bar.style.width : null,
  };
}"""


def read_state(frame):
    try:
        return frame.evaluate(_STATE_JS)
    except Exception:
        return {}


def round_is_over(frame, state=None):
    """True when the client is back to accepting a NEW bet.

    Judged on the button rather than on the wallet, because the wallet is in
    the other document and lags. Deliberately tolerant: the live round's exact
    rendering has not been observed (see the module docstring), so this asks
    the one question whose answer cannot be ambiguous -- is the control
    offering a bet again? -- rather than pattern-matching a CASH OUT label
    whose wording is a guess.
    """
    st = state if state is not None else read_state(frame)
    if not st:
        return False
    if st.get("locked") or st.get("countdown_up"):
        return False
    txt = (st.get("bet_text") or "").strip().lower()
    if not txt:
        return False
    # "BET" means idle; a staked round shows the cash-out label instead.
    return txt == "bet" and not st.get("bet_disabled")


# --- writing (controls only -- NOTHING here touches the grid) --------------

def set_mines_count(frame, n):
    """Pick the 'Mines: N' option. The dropdown offers 1..20."""
    if not 1 <= int(n) <= 20:
        raise ValueError(f"mines must be 1..20, got {n!r}")
    if read_mines_count(frame) == int(n):
        return
    frame.click("div.number-of-mines button.dropdown-toggle")
    frame.wait_for_timeout(400)
    frame.click(f"div.dropdown-list-number button.dropdown-item"
                f" >> text=/^\\s*{int(n)}\\s*$/")
    frame.wait_for_timeout(400)
    got = read_mines_count(frame)
    if got != int(n):
        raise RuntimeError(f"asked for {n} mines, the client kept {got}")


def set_stake(frame, amount):
    """Type `amount` into the bet input through the client's own numpad, then
    read it back.

    The read-back is the point, and it is the same rule
    fill_register_form()'s phone check follows: verify what LANDED, not what
    was asked for. A stake that silently stayed at the default would bet the
    wrong amount every round with nothing in the logs to show it.
    """
    want = round(float(amount), 2)
    if want <= 0:
        raise ValueError(f"stake must be positive, got {amount!r}")
    if read_stake(frame) == want:
        return want

    # Open the numpad. Its digit buttons are present but DISABLED while it is
    # closed, so clicking one without opening it is a silent no-op.
    frame.click("app-bet-spinner .dropdown-toggle")
    frame.wait_for_timeout(400)

    # Clear whatever is there: backspace is the only clear the numpad offers.
    for _ in range(12):
        if not read_stake(frame):
            break
        frame.click("app-bet-spinner app-numpad i.backspace")
        frame.wait_for_timeout(120)

    text = f"{want:.2f}".rstrip("0").rstrip(".")
    for ch in text:
        sel = ("app-bet-spinner app-numpad button >> text=/^\\s*\\.\\s*$/"
               if ch == "." else
               f"app-bet-spinner app-numpad button >> text=/^\\s*{ch}\\s*$/")
        frame.click(sel)
        frame.wait_for_timeout(120)

    frame.click("app-bet-spinner button.btn-done")
    frame.wait_for_timeout(500)

    got = read_stake(frame)
    if got != want:
        raise RuntimeError(
            f"asked to stake {want}, the client holds {got} -- refusing to bet "
            f"an amount that was not confirmed")
    return got


def place_bet(frame):
    """Click BET. Nothing else in this module clicks anything in the game."""
    frame.click("button.btn-control.btn-bet")


def _went_live(st):
    """The client is showing a round in progress (or at least not an idle BET).
    The same test wait_for_round_end has always used to decide a round began."""
    return bool(st.get("countdown_up") or st.get("locked") or (
        (st.get("bet_text") or "").strip().lower() not in ("", "bet")))


# Whatever the client is showing as a message. Spribe's refusal markup has
# never been captured, so this casts wide (any visible notification-ish
# element) -- it is evidence for the run's report, never a control signal.
_NOTICES_JS = """() => {
  const out = [];
  const sel = '[class*="notif"],[class*="toast"],[class*="alert"],' +
              '[class*="error"],[class*="snack"],[class*="message"],' +
              '[class*="popup"],[class*="modal"],[role="alert"]';
  for (const el of document.querySelectorAll(sel)) {
    const r = el.getBoundingClientRect();
    if (!r.width || !r.height) continue;
    const s = getComputedStyle(el);
    if (s.display === "none" || s.visibility === "hidden" || s.opacity === "0") continue;
    const t = (el.innerText || "").trim().replace(/\\s+/g, " ");
    if (t && t.length < 200 && !out.includes(t)) out.push(t);
    if (out.length >= 5) break;
  }
  return out;
}"""


def read_notices(target):
    """Visible message texts in a frame or page. [] when there are none."""
    try:
        return target.evaluate(_NOTICES_JS) or []
    except Exception:
        return []


def _visible_text(target, limit=600):
    try:
        return " ".join(target.inner_text("body").split())[:limit]
    except Exception as e:
        return f"(unreadable: {str(e)[:60]})"


def place_bet_confirmed(frame, game_page, amount, mines, prev_balance,
                        stop_event=None, progress=None):
    """Click BET and confirm the round actually started; re-click a bet the
    game visibly did not take.

    Returns (status, detail):
      "live"    -- a round is in progress; wait_for_round_end(saw_live=True).
      "refused" -- every try came back to an idle BET; NOTHING was staked.
      "pending" -- the button stayed disabled with no round showing; unknown.
      "moved"   -- the wallet moved while the client sat idle; not re-clicked.
      "stopped" -- /stoprun during a pause between tries; nothing staked.

    A re-click is sent only when the client is back at an enabled BET with no
    round showing AND the wallet still reads the pre-bet balance. Even a bet
    the client failed to show is a no-reveal round that auto-cashes at 1.00x,
    and the run's balance check still runs after every round.
    """
    say = progress or (lambda s: None)
    tries = len(BET_RETRY_WAITS) + 1
    notices = []
    for attempt in range(1, tries + 1):
        set_mines_count(frame, mines)
        set_stake(frame, amount)
        place_bet(frame)

        deadline = time.time() + BET_ACCEPT_SECS
        hard_stop = time.time() + ROUND_TIMEOUT_SECS
        st = {}
        while time.time() < hard_stop:
            st = read_state(frame)
            if _went_live(st):
                return "live", st
            # A disabled BET may be the client waiting on the server: never
            # click on top of it, just keep watching until the hard stop.
            if time.time() >= deadline and not st.get("bet_disabled"):
                break
            game_page.wait_for_timeout(500)
        else:
            return "pending", (f"BET stayed disabled for {ROUND_TIMEOUT_SECS:.0f}s "
                               f"with no round showing ({st})")

        notices = list(dict.fromkeys(read_notices(frame) + read_notices(game_page)))
        log.warning(f"Mines bet not taken (try {attempt}/{tries}): state={st} "
                    f"notices={notices} frame_text={_visible_text(frame)!r}")
        bal = read_wallet(game_page)
        if (bal is not None and prev_balance is not None
                and abs(bal - prev_balance) > 0.009):
            return "moved", (f"balance went {prev_balance} -> {bal} while the "
                             f"client showed no round; not re-clicking")
        if attempt == tries:
            break
        wait = BET_RETRY_WAITS[attempt - 1]
        say(f"⚠️ the game did not take the bet (try {attempt}/{tries}), nothing "
            f"staked{' — it says: ' + ' | '.join(notices) if notices else ''}; "
            f"trying again in {wait}s")
        if stop_event is not None and stop_event.wait(wait):
            return "stopped", "stopped while waiting to re-try a bet the game had not taken"

    total = int(sum(BET_RETRY_WAITS))
    return "refused", (
        f"the game did not take the bet in {tries} tries over ~{total // 60}m"
        f"{total % 60:02d}s; nothing was staked (BET still ready, balance "
        f"unchanged){'. It showed: ' + ' | '.join(notices) if notices else ''}")


def wait_for_round_end(frame, game_page, timeout_secs=ROUND_TIMEOUT_SECS,
                       stop_event=None, progress=None, saw_live=False):
    """Let the round run itself out. REVEALS NOTHING.

    Returns (ended, state). `ended` False means the round did not resolve
    inside the timeout, which is a reportable fault -- the stake may still be
    live -- not something to retry on top of. Pass saw_live=True when the
    caller has already seen the round start (place_bet_confirmed).
    """
    deadline = time.time() + timeout_secs
    told_stop = False
    while time.time() < deadline:
        if (stop_event is not None and stop_event.is_set() and not told_stop
                and progress):
            # A stop does NOT abandon the round: the countdown still resolves
            # it. Say so -- ONCE: this loop polls twice a second, and saying
            # it every pass flooded the chat into Telegram's flood control.
            progress("stop requested -- still waiting out the live round")
            told_stop = True
        st = read_state(frame)
        if _went_live(st):
            saw_live = True
        elif saw_live and round_is_over(frame, st):
            return True, st
        game_page.wait_for_timeout(500)
    return False, read_state(frame)


# --- opening the table -----------------------------------------------------

def _open_once(browser, username, password, site_url, proxy_conf, proxy,
               progress=None, built=None):
    """One full attempt: context -> WAF -> login -> launch -> live client.
    Returns (context, page, game_page, frame). Raises Walled if the GAME TAB
    was walled and a token has been cached for the next try."""
    say = progress or (lambda s: None)
    prof = m.profile_for(site_url)

    context = m.new_site_context(browser, site_url, proxy_conf)
    if built is not None:
        built.append(context)
    if prof.waf_on_navigation:
        seeded = m.seed_waf_token(context, site_url, proxy)
        say(f"🔐 WAF token seeded: {bool(seeded)}")
    page = context.new_page()
    page.goto(site_url, wait_until="domcontentloaded", timeout=60000)
    if prof.waf_on_navigation:
        page, ok, why = m.ensure_waf_cleared(page, site_url, proxy=proxy,
                                             proxy_conf=proxy_conf)
        # ensure_waf_cleared can hand back a page in a BRAND-NEW context and
        # close the old one, so track the replacement too or the retry would
        # close a context that is already gone and leak the live one.
        context = page.context
        if built is not None and context not in built:
            built.append(context)
        if not ok:
            raise RuntimeError(f"the WAF was not cleared on the way in"
                               f"{': ' + why if why else ''}")

    say("🔑 logging in")
    outcome, msgs = m.login(page, username, password, site_url,
                            already_loaded=True)
    if outcome != "ok":
        raise RuntimeError(
            f"login did not complete ({outcome})"
            f"{': ' + '; '.join(msgs)[:200] if msgs else ''}")

    say("🎰 launching Mines")
    try:
        game_page = m._open_game_direct(page, prof, TILE, site_url)
    except m.GameTabWalled as e:
        # The entry can be clean while the LAUNCH tab alone is walled, so
        # there may be no token in hand at all at this point. Mint one here
        # and let the caller replay it from a FRESH context -- a token
        # presented by the very context that was challenged is refused.
        if not m.capsolver_key():
            raise RuntimeError("the game tab is WAF-walled and "
                               "CAPSOLVER_API_KEY is not set")
        challenge = m.parse_aws_waf_challenge(e.page.content()) if e.page else None
        if not challenge:
            raise RuntimeError("the game tab is WAF-walled but its challenge "
                               "parameters could not be read")
        say("🧩 solving the game tab's wall")
        m.cache_waf_token(site_url,
                          m.solve_aws_waf_token(site_url, challenge, proxy=proxy),
                          proxy)
        raise Walled("game tab was walled; a token is cached for the retry")

    say("📡 waiting for the client")
    frame = wait_for_client(game_page)
    say("✅ table ready")
    return context, page, game_page, frame


def open_mines_table(browser, username, password, site_url, proxy_conf=None,
                     proxy=None, progress=None):
    """Open a live Mines client for `username`. Returns
    (context, page, game_page, frame); the caller owns the context.

    Two attempts, not more: on this site the scarce resource is LOGINS (the
    edge throttles them per exit IP and per account), not CapSolver credit, so
    a wall that survives one solved token means the exit IP is flagged and
    more tries only burn logins.
    """
    say = progress or (lambda s: None)
    for n in (1, 2):
        built = []
        try:
            return _open_once(browser, username, password, site_url,
                              proxy_conf, proxy, progress=say, built=built)
        except Walled as e:
            say(f"🧱 {e}")
            # Throw the challenged context away -- the cached token is only
            # accepted by a context that has carried it since its first
            # navigation.
            for c in built:
                try:
                    c.close()
                except Exception:
                    pass
            if n == 2:
                raise RuntimeError(
                    "still WAF-walled on the game tab after a solved token -- "
                    "this exit IP is flagged; use another proxy rather than "
                    "retrying")


# --- the run ---------------------------------------------------------------

def run_mines_wager(username, password, site_url, amount, rounds, mines=1,
                    proxy=None, progress=None, stop_event=None, browser=None):
    """Stake `amount` for `rounds` rounds, revealing nothing each time.

    Returns a summary dict shaped like run_paired_hedge's, so the bot's
    reporting code reads the same way:

        {ok, rounds_done, rounds_asked, stake, mines, stop_reason,
         start_balance, final_balance, rounds: [...], messages: [...]}

    The balance is expected to come back to where it started every round. Any
    DRIFT is the thing to watch: it means a round did not auto-cash at 1.00x,
    and the run stops rather than repeating whatever caused it.
    """
    say = progress or (lambda s: None)
    summary = {"ok": False, "rounds_done": 0, "rounds_asked": int(rounds),
               "stake": float(amount), "mines": int(mines),
               "stop_reason": None, "start_balance": None,
               "final_balance": None, "rounds": [], "messages": []}

    pw = own_browser = context = bridge = None
    try:
        proxy_conf = None
        if proxy:
            proxy_conf, bridge = m.maybe_bridge_proxy(m.parse_proxy(proxy))
        if browser is None:
            pw, own_browser = m._launch_pw_browser()
            browser = own_browser

        context, page, game_page, frame = open_mines_table(
            browser, username, password, site_url,
            proxy_conf=proxy_conf, proxy=proxy, progress=say)

        set_mines_count(frame, mines)
        set_stake(frame, amount)
        start = read_wallet(game_page)
        summary["start_balance"] = start
        say(f"💰 balance {start}  stake {amount}  mines {mines}")

        if start is not None and start < float(amount):
            summary["stop_reason"] = "insufficient_balance"
            summary["messages"].append(
                f"balance {start} is below the stake {amount}")
            return summary

        prev = start
        for rnd in range(1, int(rounds) + 1):
            if stop_event is not None and stop_event.is_set():
                summary["stop_reason"] = "stopped_by_user"
                break

            if not round_is_over(frame):
                # The control is not offering a bet, so do not send one -- a
                # click that lands mid-round is either ignored or, worse,
                # cashes out something. Give it a moment, then report.
                frame.wait_for_timeout(3000)
                if not round_is_over(frame):
                    summary["stop_reason"] = "client_not_ready"
                    summary["messages"].append(
                        f"round {rnd}: the BET control never came back "
                        f"({read_state(frame)})")
                    break

            # place_bet_confirmed re-asserts mines and stake before every
            # click: the client resets the stake to its default after some
            # rounds, and betting the default instead of the asked-for amount
            # is exactly the silent fault worth guarding.
            status, detail = place_bet_confirmed(
                frame, game_page, amount, mines, prev,
                stop_event=stop_event, progress=say)
            if status != "live":
                summary["stop_reason"] = {
                    "refused": "bet_not_accepted", "pending": "bet_pending",
                    "moved": "balance_moved", "stopped": "stopped_by_user",
                }[status]
                summary["messages"].append(f"round {rnd}: {detail}")
                break
            ended, state = wait_for_round_end(
                frame, game_page, stop_event=stop_event, progress=say,
                saw_live=True)
            if not ended:
                summary["stop_reason"] = "round_did_not_resolve"
                summary["messages"].append(
                    f"round {rnd}: no resolution in {ROUND_TIMEOUT_SECS:.0f}s; "
                    f"the stake may still be live -- check the account by hand "
                    f"({state})")
                break

            bal = read_wallet(game_page)
            summary["rounds"].append({"round": rnd, "balance": bal})
            summary["rounds_done"] = rnd
            say(f"✅ round {rnd}/{rounds} wagered — balance {bal}")

            # The whole premise is that a no-reveal round is a round trip. If
            # the money moved, the premise is wrong for this client and
            # repeating it would compound a real loss.
            if bal is not None and prev is not None and abs(bal - prev) > 0.009:
                summary["stop_reason"] = "balance_moved"
                summary["messages"].append(
                    f"round {rnd}: balance went {prev} -> {bal}. A no-reveal "
                    f"round is supposed to auto-cash at 1.00x and return the "
                    f"stake, so this run stopped rather than repeating it.")
                break
            prev = bal if bal is not None else prev

            if rnd < int(rounds):
                time.sleep(ROUND_GAP_SECS)

        summary["final_balance"] = read_wallet(game_page)
        summary["ok"] = summary["rounds_done"] > 0
        if summary["stop_reason"] is None:
            summary["stop_reason"] = "completed"
    except Exception as e:
        summary["stop_reason"] = summary["stop_reason"] or "error"
        summary["messages"].append(str(e)[:400])
    finally:
        for closer in (lambda: context and context.close(),
                       lambda: own_browser and own_browser.close(),
                       lambda: pw and pw.stop(),
                       lambda: bridge and m.stop_bridge(bridge)):
            try:
                closer()
            except Exception:
                pass
    return summary
