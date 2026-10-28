"""Per-site adapters for exchange.py.

exchange.py started out speaking one platform: the Laravel exchange
white-label behind khelstake24 and starexch555, where a second site was an
env file and nothing more (CLAUDE.md section 7). playinmatch99 is NOT that
platform -- it is the "jeetwin" exchange white-label, and it differs in
every route, in the betslip field list, in how the odds provider is
addressed and in how unmatched bets are listed and cancelled. Only the
Telegram UI, the accounts sheet, the admin list and the proxy pool are
genuinely shared.

So the site-specific half lives here, as one class per platform, and
exchange.py holds the parts that do not care which site it is talking to.
`site_for(url)` maps a hostname to a class, falling back to the original
Laravel one -- the same convention `sites/__init__.py` uses for the signup
engine.

Everything KhelstakeSite does is the behaviour exchange.py already had,
moved rather than rewritten, so the two production instances are unchanged.

Discovery for PlayinmatchSite was done read-only on 2026-10-28 against
playinmatch99.com's own pages and inline JavaScript; what is and is not
verified live is recorded on the class itself.
"""
from __future__ import annotations

import re
from html import unescape as html_unescape
from urllib.parse import urlsplit

import main as engine

# The odds providers 401 a request without a browser-ish header set; this is
# the UA the successful probe requests carried on both platforms.
ODDS_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36")


class ExchangeSite:
    """What exchange.py needs from a site. Subclasses fill it in.

    Methods taking an `account` get the live AccountSession, so they can use
    its `session`/`csrf`/`api_post`/`get_page`. None of them touch Telegram.
    """

    # Shown in /start when EXCHANGE_SITE_NAME is unset.
    key = "generic"
    # Sports the bot offers: key -> (label, emoji, listing path).
    sports: dict = {}
    # False when the site lists matches directly, with no series level --
    # the /event conversation then skips its series step.
    has_series = True
    # Default odds provider host for this platform (env can override).
    default_odds_host = ""

    def __init__(self, site_url: str, odds_host: str = ""):
        self.site_url = site_url.rstrip("/")
        parts = urlsplit(self.site_url)
        self.origin = f"{parts.scheme}://{parts.netloc}"
        self.odds_host = (odds_host or self.default_odds_host).rstrip("/")

    # ── auth ──
    def login(self, account) -> tuple[bool, str]:
        raise NotImplementedError

    def read_balance(self, account) -> dict | None:
        """{"balance": float|None, "exposure": float|None} or None."""
        raise NotImplementedError

    # ── navigation ──
    def list_series(self, account, sport: str) -> list[dict]:
        """[{"comp_id","name"}] -- only called when has_series."""
        raise NotImplementedError

    def list_matches(self, account, sport: str, comp_id: str | None) -> list[dict]:
        """[{"event_id","name"}]."""
        raise NotImplementedError

    def event_path(self, event_id: str, event_type: str = "4") -> str:
        raise NotImplementedError

    def load_event(self, account, event_id: str, event_type: str = "4") -> dict:
        """{"limit", "match_odds", "markets"} for one event page."""
        raise NotImplementedError

    # ── odds ──
    def market_book_url(self, event_type: str, market_ids: list[str],
                        event_id: str = "") -> str:
        raise NotImplementedError

    def parse_odds(self, data: dict) -> dict:
        """marketId -> {"status","hash","liquidity","runners"}.

        Both platforms serve Betfair-shaped MarketBooks, so this is shared.
        Index 0 of availableToBack/availableToLay is the site's level-0
        cell (read from each site's own odds-update JS), so that is what we
        surface as the best back/lay. `hash` is an encrypted blob the site
        echoes back into placeBet unchanged.
        """
        out: dict[str, dict] = {}
        for m in (data or {}).get("odds", []):
            runners = {}
            for r in m.get("runners", []):
                ex = r.get("ex") or {}
                back = (ex.get("availableToBack") or [None])[0] or {}
                lay = (ex.get("availableToLay") or [None])[0] or {}
                runners[str(r.get("selectionId"))] = {
                    "back": back.get("price"),
                    "back_size": back.get("size"),
                    "lay": lay.get("price"),
                    "lay_size": lay.get("size"),
                    "ltp": r.get("ltp"),
                    "last": r.get("lastPriceTraded"),
                }
            out[str(m.get("marketId"))] = {
                "status": m.get("status"),
                "hash": m.get("hash") or "",
                "liquidity": m.get("liquidity"),
                "runners": runners,
            }
        return out

    # ── betting ──
    def place_bet(self, account, **kw) -> dict:
        raise NotImplementedError

    def unmatched_ids(self, account) -> list[str]:
        raise NotImplementedError

    def cancel_bets(self, account, bet_ids: list[str]) -> tuple[int, list[str]]:
        """(cancelled, errors)."""
        raise NotImplementedError


# ═══════════════════════════════════════════════════════════════════════
#  khelstake24 / starexch555 -- the Laravel exchange white-label
# ═══════════════════════════════════════════════════════════════════════

class KhelstakeSite(ExchangeSite):
    """The platform exchange.py was written for. Verified live 2026-10-09/10
    (khelstake24) and 2026-10-21 (starexch555); see CLAUDE.md section 7.

    This class is the old inline code moved, not rewritten.
    """

    key = "khelstake"
    has_series = True
    default_odds_host = "https://odds4.starlingpay.co.uk"
    # Sports whose exchange pages share cricket's layout (series -> matches
    # -> a "Match Odds" market). Checked live 2026-10-15: football
    # (eventType 1, 3 runners incl. The Draw) and tennis (eventType 2) parse
    # with the same functions. Horse racing is laid out differently (no
    # series links at all) and is deliberately left out.
    sports = {
        "cricket": ("Cricket", "🏏", "/exchange/cricket-exchange"),
        "football": ("Football", "⚽", "/exchange/football-exchange"),
        "tennis": ("Tennis", "🎾", "/exchange/tennis-exchange"),
    }

    # ── auth ──
    def login(self, account) -> tuple[bool, str]:
        account.session = engine.http_session_for(account.proxy)
        account.csrf = engine.http_fetch_csrf(
            account.session, self.site_url, account.proxy)
        resp = engine.http_login_call(
            account.session, account.csrf, account.username,
            account.password, self.site_url)
        if resp.get("status") != 200:
            # status None = non-JSON/403 at the edge, not an app refusal.
            if resp.get("status") is None:
                return False, f"edge blocked: {(resp.get('message') or '')[:80]}"
            return False, resp.get("message") or f"Login failed: {resp}"
        return True, ""

    def read_balance(self, account) -> dict | None:
        resp = engine.http_get_balance(account.session, account.csrf, self.site_url)
        if _session_expired(resp):
            return {"_expired": True}
        if resp.get("status") != 200:
            return None
        bal = resp.get("balance") or {}
        wallet = bal.get("wallet")
        if wallet is None:
            # starexch has no balance.wallet key; totalBalance/balance is
            # what makes that site work (CLAUDE.md section 7).
            wallet = _as_float(bal.get("totalBalance") or bal.get("balance"))
        return {"balance": _as_float(wallet), "exposure": _as_float(bal.get("exposure"))}

    # ── navigation ──
    def list_series(self, account, sport: str) -> list[dict]:
        path = self.sports[sport][2]
        return parse_competitions(account.get_page(path))

    def list_matches(self, account, sport: str, comp_id: str | None) -> list[dict]:
        return parse_match_list(account.get_page(f"/ex/competition/{comp_id}"))

    def event_path(self, event_id: str, event_type: str = "4") -> str:
        return f"/ex/event-details/{event_id}"

    def load_event(self, account, event_id: str, event_type: str = "4") -> dict:
        return parse_match_page(account.get_page(self.event_path(event_id)))

    # ── odds ──
    def market_book_url(self, event_type: str, market_ids: list[str],
                        event_id: str = "") -> str:
        q = ",".join(market_ids[:5])
        return (f"{self.odds_host}/api/v1/get/getMarketBook/{event_type}"
                f"?q={q}&api_mode=1&eventType={event_type}")

    # ── betting ──
    def place_bet(self, account, *, event_id, event_type, market_id,
                  market_name, runner_id, runner_name, teams, odds, stake,
                  side, matched="", ltp="", limit="", liquidity=None) -> dict:
        """Payload mirrors the site's own betslip form serialize() exactly
        (field list and value sources confirmed from the page's inline JS
        2026-10-09): bet_type is availableToBack/availableToLay (the ladder
        side), betting_type is Back/Lay (the slip label), profit =
        stake*(odds-1) for both sides (liability on Lay), matched = the
        market's hash blob, ltp = the runner's ltp blob (page default "na"
        when absent), expo = 0 (the site's own default for a new bet).
        """
        bet_type = "availableToBack" if side == "back" else "availableToLay"
        data = {
            "teams": teams,
            "market_name": market_name or "Match Odds",
            "runnerName": runner_name,
            "eventId": event_id,
            "odds": f"{odds:.2f}",
            "runnerId": runner_id,
            "eventType": event_type or "4",
            "market_type": market_id,
            "ltp": ltp or "na",
            "stake": f"{stake:g}",
            "matched": matched or "",
            "expo": "0",
            "limit": limit,
            "betting_type": "Back" if side == "back" else "Lay",
            "profit": f"{stake * (odds - 1):.2f}",
            "bet_type": bet_type,
        }
        resp = account.api_post("/api2/v2/placeBet", data)
        return _judge_place(resp, account, routine_check_status=205)

    def unmatched_ids(self, account) -> list[str]:
        """Bet ids of every currently-unmatched bet, via the site's own
        /unmatchedbetdetails panel (HTML). Extracted the same way the site's
        cancel_unmatcedbet() JS reads them: the cancel control carries
        id=<betId>, and rows carry class unmatched_bet_<betId>."""
        resp = account.api_post("/unmatchedbetdetails", {"bets_name": "unmatched"})
        if resp.get("_error"):
            raise RuntimeError(resp.get("message") or "unmatchedbetdetails failed")
        res = resp.get("res") or ""
        ids: list[str] = []
        for m in re.finditer(r"<\w+[^>]*\bcancel_unmatcedbet\b[^>]*>", res):
            tag = m.group(0)
            im = re.search(r'\bid=["\']([^"\']+)["\']', tag)
            # edit_<id> buttons open the partial-edit UI, never cancel.
            if im and not im.group(1).startswith("edit_"):
                ids.append(im.group(1))
        ids.extend(re.findall(r"unmatched_bet_(\d+)", res))
        return _dedupe(ids)

    def cancel_bets(self, account, bet_ids: list[str]) -> tuple[int, list[str]]:
        """No cancel-all endpoint exists here -- the site's own UI cancels
        one id at a time, so we loop. Response key is statusCode, not
        status (placeBet is the other way round)."""
        errors = []
        for bid in bet_ids:
            try:
                resp = account.api_post("/api2/v2/cancelbet", {"id": bid},
                                        params={"desktop": "1"})
                ok = resp.get("statusCode") == 200
                msg = resp.get("message") or str(resp)[:100]
            except Exception as exc:
                ok, msg = False, str(exc)[:80]
            if not ok:
                errors.append(f"{bid}: {msg[:40]}")
        return len(bet_ids) - len(errors), errors


# ═══════════════════════════════════════════════════════════════════════
#  playinmatch99 -- the "jeetwin" exchange white-label
# ═══════════════════════════════════════════════════════════════════════

class PlayinmatchSite(ExchangeSite):
    """playinmatch99.com. A different platform from khelstake/starexch, not
    a skin of it -- mapped read-only 2026-10-28 from its own pages and
    inline JS. What differs, and why each line below is the way it is:

    * **There is no series level.** Matches are listed flat on
      `/mobile/sports` as `<li id="highlightEvent_<eventId>" class="…
      event_<sportId>…">` with the name in `<dd id="eventName">`, grouped
      only by a CSS class per sport. So `has_series` is False and the
      /event conversation goes sport -> match directly. `/exchange/...`,
      `/ex/competition/...` and `/ex/event-details/...` all 404 here.
    * **Event pages are `/mobile/eventDetails/<id>?type=<eventType>`.**
      Markets are `<div class="markets_" data-marketid=…>`; each runner is
      a `<dl id="selection_<runnerId>" data-marketid data-runnerid>` row
      holding its Back and Lay `<a class="odds_select back|lay">` cells.
    * **The odds provider is a different host AND a different URL shape**:
      `odds.funoddsin.com/api/v1/get/getMarketBook/<eventId>?q=<marketIds>`
      -- the EVENT id is in the path here, where khelstake puts the event
      TYPE. The response body is the same Betfair shape, so `parse_odds`
      is inherited. Verified live anonymously: 200, OPEN book, real prices.
    * **The betslip has its own field list**, read out of the page's own
      `var html = '<form id="placeBet_">'` template: it carries `exposure`
      and `vol` and has no `ltp` or `profit`. `vol` is the market's
      `liquidity` and `matched` is the market `hash`, both copied from the
      odds response by the site's own update handler -- the same
      convention khelstake uses for `matched`.
      **The `limit` blob is the one in the form's SUBMIT handler, not the
      one in the template**, and the template emits the field twice. On
      submit the page runs `find('input[name=limit]').val("<blob>")`, which
      matches BOTH inputs and sets both, so the server receives the
      submit-handler's blob twice -- a different value from the one the
      template seeded. Taking the template's blob (the obvious reading)
      would send a value this site never sends, and the duplicate field is
      reproduced rather than "cleaned up" because we do not get to decide
      which copy the server reads.
    * **bet_type / betting_type follow the same convention as khelstake**
      (`availableToBack`/`availableToLay` vs `Back`/`Lay`), confirmed from
      the `.odds_select` click handler.
    * **Login is `/api2/v2/login` with email/password/digits**, not the
      Laravel `/login` form. `digits` is a four-digit number the page
      generates client-side in `authenticate()` and compares to itself, so
      it cannot be a server-side secret; we send a random one, as the page
      does.
    * **Unmatched bets come back as JSON**, not scraped HTML:
      `/api2/v2/getBetsHistory` -> `data[]` with `id`, `teams`,
      `market_name`, `sizeMatched`, `sizeRemaining`. Unmatched means
      `sizeRemaining > 0`.
    * **Cancelling is bulk**: `/api2/v2/cancelAllBet` takes an `id[]` array,
      so /cancelall is one request per account instead of one per bet.
    * **"Routine check" is status 404 here, not 205** -- the site redirects
      itself to `/users/logout?q=routinecheck` on it, same as khelstake does
      on its 205.

    ⚠️ **Nothing below has been exercised with a real account.** The routes,
    the payload shapes and the response conventions were read out of the
    site's own JavaScript and its pages, and the odds provider was called
    live; but login, placeBet, getBetsHistory and cancelAllBet all need
    credentials this repo does not have. Run the confirm screen's
    **🧪 Test 1 ID** (an unmatched BACK @1.01, cancelled immediately -- no
    exposure) on one funded ID before any multi-ID bet, exactly as
    khelstake and starexch were gated.
    """

    key = "playinmatch"
    has_series = False
    default_odds_host = "https://odds.funoddsin.com"
    # The listing page is one page for every sport; the sport is a CSS class
    # on each row (event_<id>), so the path repeats and the id selects.
    # 4 = cricket, 1 = soccer, 2 = tennis -- read off the page's own
    # `event_list event_4` classes and its `sportsBookEIcon_<n>` labels.
    sports = {
        "cricket": ("Cricket", "🏏", "/mobile/sports"),
        "football": ("Football", "⚽", "/mobile/sports"),
        "tennis": ("Tennis", "🎾", "/mobile/sports"),
    }
    SPORT_EVENT_TYPE = {"cricket": "4", "football": "1", "tennis": "2"}

    # ── auth ──
    def login(self, account) -> tuple[bool, str]:
        import random
        account.session = engine.http_session_for(account.proxy)
        # The csrf meta tag lives on every page; /mobile is the cheapest.
        account.csrf = self._csrf(account, "/mobile")
        if not account.csrf:
            return False, "could not read the csrf token (site blocked the page?)"
        resp = account.api_post("/api2/v2/login", {
            "email": account.username,
            "password": account.password,
            # Client-side-only captcha, see the class docstring.
            "digits": f"{random.randint(0, 9999):04d}",
        })
        if resp.get("_error"):
            return False, f"edge blocked: {(resp.get('message') or '')[:80]}"
        if resp.get("status") != 200:
            return False, resp.get("message") or f"Login failed: {resp}"
        return True, ""

    def _csrf(self, account, path: str) -> str:
        r = account.session.get(
            f"{self.origin}{path}",
            headers={"User-Agent": ODDS_UA}, timeout=45)
        m = re.search(r'<meta name="csrf-token" content="([^"]+)"', r.text)
        return m.group(1) if m else ""

    def read_balance(self, account) -> dict | None:
        resp = account.api_post("/api2/v2/getBalance")
        if _session_expired(resp):
            return {"_expired": True}
        if resp.get("status") != 200:
            return None
        bal = resp.get("balance") or resp.get("data") or {}
        if not isinstance(bal, dict):
            return None
        raw = bal.get("wallet")
        if raw is None:
            raw = bal.get("balance") or bal.get("totalBalance")
        return {"balance": _as_float(raw), "exposure": _as_float(bal.get("exposure"))}

    # ── navigation ──
    def list_series(self, account, sport: str) -> list[dict]:
        return []    # has_series is False; never called

    def list_matches(self, account, sport: str, comp_id: str | None) -> list[dict]:
        html = account.get_page(self.sports[sport][2])
        return parse_highlight_list(html, self.SPORT_EVENT_TYPE.get(sport, "4"))

    def event_path(self, event_id: str, event_type: str = "4") -> str:
        return f"/mobile/eventDetails/{event_id}?type={event_type or '4'}"

    def load_event(self, account, event_id: str, event_type: str = "4") -> dict:
        html = account.get_page(self.event_path(event_id, event_type))
        # The csrf token is per-page here and placeBet is judged by it, so
        # take the one this very page carries rather than the login's.
        m = re.search(r'<meta name="csrf-token" content="([^"]+)"', html)
        if m:
            account.csrf = m.group(1)
        return parse_event_page(html)

    # ── odds ──
    def market_book_url(self, event_type: str, market_ids: list[str],
                        event_id: str = "") -> str:
        q = ",".join(market_ids[:5])
        return (f"{self.odds_host}/api/v1/get/getMarketBook/{event_id}"
                f"?q={q}&api_mode=1&eventType={event_type}&uid=")

    # ── betting ──
    def place_bet(self, account, *, event_id, event_type, market_id,
                  market_name, runner_id, runner_name, teams, odds, stake,
                  side, matched="", ltp="", limit="", liquidity=None) -> dict:
        bet_type = "availableToBack" if side == "back" else "availableToLay"
        # Field order follows the site's own form template, and `limit`
        # appears twice exactly as the template emits it -- see the class
        # docstring. A list of pairs is the only way to send a repeated key.
        data = [
            ("eventType", event_type or "4"),
            ("eventId", event_id),
            ("runnerId", runner_id),
            ("market_type", market_id),
            ("exposure", "0"),
            ("vol", str(liquidity) if liquidity is not None else "1"),
            ("matched", matched or ""),
            ("limit", limit),
            ("limit", limit),
            ("expo", "0"),
            ("bet_type", bet_type),
            ("market_name", market_name or "Match Odds"),
            ("runnerName", runner_name),
            ("teams", teams),
            ("stake", f"{stake:g}"),
            ("betting_type", "Back" if side == "back" else "Lay"),
            ("odds", f"{odds:.2f}"),
        ]
        resp = account.api_post("/api2/v2/placeBet", data)
        return _judge_place(resp, account, routine_check_status=404)

    def unmatched_ids(self, account) -> list[str]:
        """JSON here, not scraped HTML. A bet is unmatched while it still
        has size remaining -- the same test the site's own getOpenBets()
        uses to tag a row 'unmatched'."""
        resp = account.api_post("/api2/v2/getBetsHistory")
        if resp.get("_error"):
            raise RuntimeError(resp.get("message") or "getBetsHistory failed")
        rows = resp.get("data") or []
        if isinstance(rows, dict):
            rows = list(rows.values())
        ids = [str(r.get("id")) for r in rows
               if isinstance(r, dict) and r.get("id") is not None
               and _as_float(r.get("sizeRemaining")) and
               _as_float(r.get("sizeRemaining")) > 0]
        return _dedupe(ids)

    def cancel_bets(self, account, bet_ids: list[str]) -> tuple[int, list[str]]:
        """One request cancels the lot: cancelAllBet takes an id[] array
        (read from the site's .cancel_unmatched handler, which sends a
        one-element array for a single row)."""
        if not bet_ids:
            return 0, []
        data = [(f"id[{i}]", bid) for i, bid in enumerate(bet_ids)]
        try:
            resp = account.api_post("/api2/v2/cancelAllBet", data)
        except Exception as exc:
            return 0, [f"cancelAllBet: {str(exc)[:60]}"]
        if resp.get("status") == 200:
            return len(bet_ids), []
        return 0, [resp.get("message") or str(resp)[:80]]


# ═══════════════════════════════════════════════════════════════════════
#  Parsers -- pure functions, so they can be tested against saved HTML
# ═══════════════════════════════════════════════════════════════════════

def parse_competitions(html: str) -> list[dict]:
    """Series list from khelstake's /exchange/<sport>-exchange.

    Named series are CompetitionRow links whose anchor contains a plain
    <span>Name</span>. The per-section "See All" links carry no span name,
    so they drop out naturally. Deduped by comp id (a series can appear in
    more than one section).
    """
    out, seen = [], set()
    for m in re.finditer(
            r'href="[^"]*/ex/competition/(\d+)"[^>]*>\s*<span>([^<]+)</span>',
            html):
        cid, name = m.group(1), html_unescape(m.group(2)).strip()
        if not name or name.lower() == "see all" or cid in seen:
            continue
        seen.add(cid)
        out.append({"comp_id": cid, "name": name})
    return out


def parse_match_list(html: str) -> list[dict]:
    """Matches from a khelstake cricket page or /ex/competition/{id} page.

    Team names live in the anchor's team_names_box span; the box's own
    closing </span> is found by looking for what follows it (the
    main_event_module div, still inside the anchor) so the inner "vs" span
    can't cut the name short.
    """
    out, seen = [], set()
    for m in re.finditer(
            r'<a[^>]*href="[^"]*/ex/event-details/(\d+)"[^>]*>(.*?)</a>',
            html, re.S):
        eid, inner = m.group(1), m.group(2)
        if eid in seen:
            continue
        box = re.search(
            r'team_names_box[^>]*>(.*?)</span>\s*'
            r'(?:<div class="main_event_module|</a>)',
            inner, re.S)
        txt = html_unescape(re.sub(r"<[^>]+>", " ", box.group(1) if box else inner))
        name = re.sub(r"\s+", " ", txt).strip() or f"Match {eid}"
        seen.add(eid)
        out.append({"event_id": eid, "name": name})
    return out


def parse_match_page(html: str) -> dict:
    """Market structure from khelstake's /ex/event-details/{eventId}.

    Odds cells are <a class="...betslip_details..." data-runnerId=...>;
    their data-odd values are placeholders ("1") -- real odds come from the
    provider. Also pulls the page-level encrypted "limit" blob the site
    serializes into every placeBet.
    """
    lim = re.search(r'name="limit"\s+type="hidden"\s+value="([^"]+)"', html)
    markets: dict[str, dict] = {}
    for m in re.finditer(r"<a\b[^>]*data-runnerId=\"[^\"]*\"[^>]*>", html):
        d = dict(re.findall(r'(data-[a-zA-Z_]+)="([^"]*)"', m.group(0)))
        rid, mid = d.get("data-runnerId"), d.get("data-market_type")
        if not rid or not mid:
            continue
        mk = markets.setdefault(mid, {
            "market_id": mid,
            "market_name": html_unescape(d.get("data-market_name") or ""),
            "event_id": d.get("data-eventId") or "",
            "event_type": d.get("data-eventtype") or "4",
            "teams": html_unescape(d.get("data-teams") or ""),
            "runners": {},
        })
        mk["runners"].setdefault(
            rid, html_unescape(d.get("data-runner_name") or f"Runner {rid}"))
    return {
        "limit": lim.group(1) if lim else "",
        "match_odds": _pick_match_odds(markets),
        "markets": list(markets.values()),
    }


def parse_highlight_list(html: str, event_type: str = "4") -> list[dict]:
    """Matches from playinmatch's /mobile/sports.

    Every sport is on the one page; a row carries its sport as the CSS
    class `event_<eventType>`, so the caller's sport selects here rather
    than in the URL. The name is the row's `<dd id="eventName">`.
    """
    out, seen = [], set()
    for m in re.finditer(
            r'<li[^>]*id="highlightEvent_(\d+)"[^>]*class="([^"]*)"[^>]*>(.*?)</li>',
            html, re.S):
        eid, classes, inner = m.group(1), m.group(2), m.group(3)
        if eid in seen:
            continue
        if not re.search(rf"\bevent_{re.escape(event_type)}\b", classes):
            continue
        nm = re.search(r'<dd[^>]*id="eventName"[^>]*>(.*?)</dd>', inner, re.S)
        name = re.sub(r"\s+", " ",
                      html_unescape(re.sub(r"<[^>]+>", " ", nm.group(1) if nm else ""))
                      ).strip() or f"Match {eid}"
        seen.add(eid)
        out.append({"event_id": eid, "name": name})
    return out


def parse_event_page(html: str) -> dict:
    """Market structure from playinmatch's /mobile/eventDetails/{id}.

    Runner rows are `<dl id="selection_<runnerId>" data-marketid=…
    data-runnerid=…>`; the Back/Lay `<a class="odds_select">` cells inside
    carry the market and runner names. data-odd is a placeholder ("1") and
    data-bet_type is "1" in the static markup -- both are rewritten by the
    page's own odds-refresh handler, so neither is read here. The real
    prices come from the provider, and the side comes from the user.

    The encrypted `limit` blob is read from the form's SUBMIT handler, not
    from the betslip template. Both exist and they are DIFFERENT blobs: the
    template seeds `limit` with one, and then on submit the page does
    `$('#placeBet_').find('input[name=limit]').val("<another blob>")` --
    which, because jQuery's find() matches both of the template's limit
    inputs, overwrites them both. So what the server actually receives is
    the submit-handler's blob, twice, and that is what place_bet sends.
    Reading the template's blob instead looks perfectly reasonable and
    would send a value the site never sends.
    """
    lim = re.search(r"input\[name=limit\]'\)\.val\(\s*[\"']([A-Za-z0-9+/=]{20,})[\"']",
                    html)
    if not lim:
        # Fallback: the template's seed blob, better than sending nothing.
        lim = re.search(r"name=\\?[\"']limit\\?[\"']\s+type=\\?[\"']hidden\\?[\"']"
                        r"\s+value=\\?[\"']([A-Za-z0-9+/=]{20,})\\?[\"']", html)
    markets: dict[str, dict] = {}
    for m in re.finditer(r"<a\b[^>]*\bodds_select\b[^>]*>", html):
        d = {k.lower(): v for k, v in
             re.findall(r'(data-[a-zA-Z_]+)="([^"]*)"', m.group(0))}
        rid = d.get("data-runnerid")
        mid = d.get("data-market_type") or d.get("data-marketid")
        if not rid or not mid or not mid.startswith("1."):
            continue
        mk = markets.setdefault(mid, {
            "market_id": mid,
            "market_name": html_unescape(d.get("data-market_name") or ""),
            "event_id": d.get("data-eventid") or "",
            "event_type": d.get("data-eventtype") or "4",
            "teams": html_unescape(d.get("data-teams") or ""),
            "runners": {},
        })
        mk["runners"].setdefault(
            rid, html_unescape(d.get("data-runner_name") or f"Runner {rid}"))
    return {
        "limit": lim.group(1) if lim else "",
        "match_odds": _pick_match_odds(markets),
        "markets": list(markets.values()),
    }


def _pick_match_odds(markets: dict) -> dict | None:
    """The Match Odds market, or None.

    Named exactly "Match Odds" on both platforms. Nothing else is
    substituted: betting the wrong market is worse than refusing, and the
    caller tells the operator which markets it did see.
    """
    return next((mk for mk in markets.values()
                 if mk["market_name"].strip().lower() == "match odds"), None)


# ═══════════════════════════════════════════════════════════════════════
#  Shared helpers
# ═══════════════════════════════════════════════════════════════════════

def _as_float(raw):
    try:
        return float(str(raw).replace(",", "").replace("₹", "").strip())
    except (TypeError, ValueError):
        return None


def _dedupe(items):
    seen, out = set(), []
    for i in items:
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out


def _session_expired(resp: dict) -> bool:
    """The site's answer once a login session has ended (Laravel 419)."""
    msg = str((resp or {}).get("message") or "")
    return "CSRF token mismatch" in msg or "Non-JSON (419)" in msg


def _judge_place(resp: dict, account, *, routine_check_status: int) -> dict:
    """Read a placeBet reply. Both platforms answer `status`, and both have
    a status that means "this account is under routine check" -- 205 on
    khelstake, 404 on playinmatch, each followed by the site redirecting
    itself to /logout?q=routinecheck. Flag the account either way: retrying
    bets on one the site has logged out only digs the hole."""
    status = resp.get("status")
    if status == routine_check_status:
        account.flagged = True
        return {"ok": False, "flagged": True,
                "message": "account placed under routine check (needs review)"}
    if status == 200:
        return {"ok": True,
                "message": resp.get("message") or "Bet placed successfully"}
    if resp.get("_error"):
        return {"ok": False, "message": resp.get("message") or "request failed"}
    return {"ok": False, "message": (resp.get("message") or str(resp)[:100])}


# ═══════════════════════════════════════════════════════════════════════
#  Selection
# ═══════════════════════════════════════════════════════════════════════

_BY_HOST = {
    "playinmatch99.com": PlayinmatchSite,
    "playinmatch.com": PlayinmatchSite,
    "khelstake24.com": KhelstakeSite,
    "starexch555.com": KhelstakeSite,
}


def site_for(site_url: str, odds_host: str = "") -> ExchangeSite:
    """Pick the adapter for a site URL by hostname.

    Unknown hosts fall back to the Laravel white-label, which is the one
    most of these sites run and the one every parser was written for. A
    site on the jeetwin template needs a line in _BY_HOST -- the tell is
    `/mobile/sports` answering 200 while `/exchange/cricket-exchange` 404s.
    """
    host = urlsplit(site_url).netloc.lower().replace("www.", "")
    cls = _BY_HOST.get(host, KhelstakeSite)
    return cls(site_url, odds_host)
