#!/usr/bin/env python3
"""exchange.py -- Multi-ID exchange bet bot (khelstake24, starexch555).

A standalone Telegram bot (its own EXCHANGE_BOT_TOKEN, NOT a BOT_MODE of
telegram_bot.py) that places the same Back/Lay Match Odds bet on every
operator ID at once, cancels unmatched bets, and refreshes balances.

ONE PROCESS PER SITE, picked entirely by env -- there is no per-site code
here. khelstake24.com and starexch555.com run the SAME exchange
white-label: verified live 2026-10-21 against starexch555 that every route,
parser and rule below is byte-compatible (same /exchange/*-exchange and
/ex/* pages, the same page-level encrypted "limit" blob, the same
odds4.starlingpay.co.uk provider, the same min stake 2000 / max 5,000,000
and odds 1.01-5.0 rules read out of its own inline JS). parse_competitions
/ parse_match_list / parse_match_page were each run against real starexch
HTML unchanged, and a real account logged in, read its balance + exposure
and listed its (empty) unmatched panel over plain HTTP. So a third site on
this template needs an env file, not a fork -- confirm it with
probe_exchange.py first.

Architecture (every line below confirmed live 2026-10-09 via probe_exchange.py
against real pages/accounts -- the site's own inline JS was read for the
exact request shapes):
  * Series/matches are SERVER-RENDERED HTML, not JSON APIs:
      GET /exchange/{cricket,football,tennis}-exchange -> series list
      GET /ex/competition/{compId}       -> matches of one series
      GET /ex/event-details/{eventId}    -> match page, market cells + limit blob
  * Live odds come from an EXTERNAL provider (Betfair-style):
      POST https://odds4.starlingpay.co.uk/api/v1/get/getMarketBook/{eventType}
          ?q={marketIds (<=5, comma)}&api_mode=1&eventType={eventType}
      Needs browser headers (Origin/Referer/UA/X-CSRF-TOKEN) -- a bare request
      gets 401 from their CheckDomain middleware. Response: odds[].runners[]
      with ex.availableToBack/availableToLay [{price,size}] (index 0 = the
      site's level-0 cell), market hash blob, runner.ltp blob.
  * Bet placement is a Laravel form POST, byte-identical to the site's own
    $(form).serialize() of its betslip template:
      POST /api2/v2/placeBet
      fields: teams, market_name, runnerName, eventId, odds, runnerId,
              eventType, market_type, ltp, stake, matched, expo, limit,
              betting_type, profit, bet_type, _token
      status 200 = placed; status 205 = "account under routine check"
      (the site then logs itself out via /logout?q=routinecheck) -> flagged.
  * Unmatched bets:
      POST /unmatchedbetdetails  data {bets_name:"unmatched", _token}
      -> {res: "<html rows>"}; empty account returns a "No Unmatched Bets"
      div. Cancel buttons carry id=<betId> and onclick=cancel_unmatcedbet.
  * Cancel ONE bet (the site has no cancel-all endpoint):
      POST /api2/v2/cancelbet?desktop=1  data {id: <betId>, _token}
      -> statusCode == 200 (note: "statusCode", unlike placeBet's "status").

Site rules enforced client-side here because the server reuses one generic
refusal message ("Sorry the bet cannot be accepted by our risk management
team") for both violations -- read from the site's own JS 2026-10-09:
  * stake >= 2000 (env EXCHANGE_MIN_STAKE) -- the SERVER enforces this too,
    verbatim "Min Stake is 2000" (probed live 2026-10-10).
  * Match Odds: 1.01 <= odds <= 5.0
  * profit field = stake * (odds - 1), 2dp, for Back AND Lay (on Lay it is
    the liability -- the site labels it that way but sends the same formula).

Server-side gate order, each observed live 2026-10-10 with this exact
payload (so a refusal names the gate that fired -- the payload itself is
accepted up to it):
  1. market liquidity: "Bet rejected due to low liquidity, try again later !"
     -- a MARKET-level gate (a women's-league match refused; the same odds
     on a 19M-liquidity market passed it), not an odds-level one.
  2. min stake: "Min Stake is 2000"
  3. balance: "Insufficient funds. Please deposit."
  4. status 205: "account under routine check" -> flagged (see below).

The "limit" field is a page-level encrypted blob (one per event page load,
same for every cell) -- extracted from the hidden input on the match page.

Test bet (the client-approved smoke test): the confirm screen's
"🧪 Test 1 ID" places an unmatched BACK @1.01 on the first logged-in ID and
cancels the new unmatched bet straight away. At 1.01 nothing can match (a
match would need a lay priced <= 1.01), so there is no exposure -- it exists
to prove the full place->list->cancel round trip on the real server before
volume. The probe account holds only ~1116 so the server refuses it at the
balance gate ("Insufficient funds"); run the test once on an account funded
>= 2000 to complete the round trip.

Commands:
  /event      -- pick sport -> browse series -> match -> Back/Lay side -> odds+amount ->
                 confirm (place on all IDs / test on 1 ID / cancel)
  /cancelall  -- cancel every unmatched bet on every ID
  /refresh    -- balances + exposure for every ID
  /ids        -- list accounts and status
  /relogin    -- reload accounts from the sheet and re-login
  /addadmin /removeadmin -- master only; persisted to admins.exchange.json

Run:
    .venv/bin/python exchange.py --env .env.exchange
"""
from __future__ import annotations

import asyncio
import functools
import itertools
import json
import logging
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from html import unescape as html_unescape
from urllib.parse import urlsplit

# ── Env ──
_env_file = ".env.exchange"
if "--env" in sys.argv:
    _idx = sys.argv.index("--env")
    if _idx + 1 < len(sys.argv):
        _env_file = sys.argv[_idx + 1]

from dotenv import load_dotenv
load_dotenv(_env_file, override=True)
load_dotenv(".env")

import gspread
from google.oauth2.service_account import Credentials
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler,
    ContextTypes, ConversationHandler, MessageHandler, filters,
)

import main as engine

# ── Logging ──
logging.basicConfig(
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    level=logging.INFO)
logger = logging.getLogger("exchange")

# ── Config ──
BOT_TOKEN = os.environ.get("EXCHANGE_BOT_TOKEN", "")
SITE_URL = os.environ.get("EXCHANGE_SITE_URL", "https://khelstake24.com")
# Shown in /start. Defaults to the site's hostname without www/TLD noise, so
# a new site needs no code change -- only an env file.
SITE_NAME = os.environ.get("EXCHANGE_SITE_NAME", "") or (
    urlsplit(SITE_URL).netloc.replace("www.", "").split(".")[0] or "exchange")
SPREADSHEET_ID = os.environ.get("EXCHANGE_SHEET_ID", "")
WORKSHEET_NAME = os.environ.get("EXCHANGE_WORKSHEET_NAME", "")
# A tab can be named either way: the gid is what a sheet URL carries
# (…/edit?gid=1162463240), so it survives a tab rename. It wins over the
# name when both are set.
WORKSHEET_GID = os.environ.get("EXCHANGE_WORKSHEET_GID", "").strip()
CREDENTIALS_FILE = os.environ.get("SHEET_CREDENTIALS_FILE", "service_account.json")
MASTER_ADMIN_IDS = set(
    os.environ.get("MASTER_ADMIN_ID", "").replace(",", " ").split())
PROXY_FILE = os.environ.get("EXCHANGE_PROXY_FILE", "proxies.txt")
BET_CONCURRENCY = int(os.environ.get("EXCHANGE_BET_CONCURRENCY", "5"))
ADMINS_FILE = os.environ.get("ADMINS_FILE", "admins.exchange.json")

# Site rules, read from the site's own JS 2026-10-09 (see module docstring).
MIN_STAKE = int(os.environ.get("EXCHANGE_MIN_STAKE", "2000"))
MIN_ODDS = float(os.environ.get("EXCHANGE_MIN_ODDS", "1.01"))
MAX_ODDS = float(os.environ.get("EXCHANGE_MAX_ODDS", "5.0"))

# The odds provider 401s requests without a browser-ish header set; this is
# the UA the successful probe request carried.
_ODDS_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36")
# Both sites verified so far use this same provider; env'd so a third one
# that points elsewhere needs no code change.
ODDS_HOST = os.environ.get(
    "EXCHANGE_ODDS_HOST", "https://odds4.starlingpay.co.uk").rstrip("/")


# ── Proxy pool ──
def _load_proxies():
    if not os.path.exists(PROXY_FILE):
        return []
    with open(PROXY_FILE) as f:
        return [l.strip() for l in f if l.strip() and not l.startswith("#")]

_proxies = _load_proxies()
_proxy_idx = itertools.cycle(range(len(_proxies))) if _proxies else None

def next_proxy():
    if _proxy_idx is None:
        return None
    return _proxies[next(_proxy_idx)]


# ── Auth ──
admin_ids: set[str] = set()

def _load_admins():
    try:
        with open(ADMINS_FILE) as f:
            return {str(x) for x in json.load(f)}
    except (OSError, ValueError):
        return set()

def _save_admins():
    try:
        with open(ADMINS_FILE, "w") as f:
            json.dump(sorted(admin_ids), f)
    except OSError as exc:
        logger.warning("Could not persist admins: %s", exc)

def is_master(uid):
    return str(uid) in MASTER_ADMIN_IDS

def is_admin(uid):
    return is_master(uid) or str(uid) in admin_ids

def require_admin(func):
    @functools.wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user is None or update.message is None:
            return
        uid = update.effective_user.id
        if not is_admin(uid):
            await update.message.reply_text(
                f"⛔ Not authorized.\nYour ID: `{uid}`\n"
                f"Ask the owner to run `/addadmin {uid}`",
                parse_mode="Markdown")
            return
        return await func(update, context)
    return wrapper


# ═══════════════════════════════════════════════════════════════════════
#  ACCOUNT SESSION
# ═══════════════════════════════════════════════════════════════════════

def _session_expired(resp: dict) -> bool:
    """The site's answer once a login session has ended (Laravel 419)."""
    msg = str((resp or {}).get("message") or "")
    return "CSRF token mismatch" in msg or "Non-JSON (419)" in msg


class AccountSession:
    """One exchange account, logged in over plain HTTP (no browser)."""

    def __init__(self, username: str, password: str, sheet_row: int | None = None):
        self.username = username
        self.password = password
        self.sheet_row = sheet_row    # 1-based row in the accounts sheet
        self.session = None
        self.csrf: str | None = None
        self.balance: float | None = None
        self.exposure: float | None = None
        self.logged_in = False
        self.flagged = False   # placeBet answered 205: "routine check"
        self.error: str | None = None
        self.proxy: str | None = None

    def login(self) -> bool:
        self.proxy = next_proxy()
        try:
            self.session = engine.http_session_for(self.proxy)
            self.csrf = engine.http_fetch_csrf(self.session, SITE_URL, self.proxy)
            resp = engine.http_login_call(
                self.session, self.csrf, self.username, self.password, SITE_URL)
            if resp.get("status") != 200:
                # status None = non-JSON/403 at the edge, not an app refusal.
                if resp.get("status") is None:
                    self.error = f"edge blocked: {(resp.get('message') or '')[:80]}"
                else:
                    self.error = resp.get("message") or f"Login failed: {resp}"
                self.logged_in = False
                return False
            self.logged_in = True
            self.error = None
            self.refresh_balance(_retried=True)
            return True
        except Exception as exc:
            self.error = str(exc)[:200]
            self.logged_in = False
            return False

    def refresh_balance(self, _retried: bool = False):
        if not self.logged_in or not self.session:
            return
        try:
            resp = engine.http_get_balance(self.session, self.csrf, SITE_URL)
            if _session_expired(resp) and not _retried:
                # Otherwise the old figure stays on screen as if current.
                logger.info("Session expired for %s, logging in again", self.username)
                if self.login():   # login() refreshes the balance itself
                    return
            if resp.get("status") == 200:
                bal = resp.get("balance") or {}
                wallet = bal.get("wallet")
                if wallet is None:
                    raw = bal.get("totalBalance") or bal.get("balance")
                    try:
                        wallet = float(str(raw).replace(",", ""))
                    except (TypeError, ValueError):
                        wallet = None
                self.balance = float(wallet) if wallet is not None else None
                exp = bal.get("exposure")
                if exp is not None:
                    try:
                        self.exposure = float(str(exp).replace(",", ""))
                    except (TypeError, ValueError):
                        self.exposure = None
        except Exception as exc:
            logger.warning("Balance refresh failed for %s: %s", self.username, exc)

    def _headers(self, referer: str | None = None) -> dict:
        parts = urlsplit(SITE_URL)
        origin = f"{parts.scheme}://{parts.netloc}"
        return {
            "X-Requested-With": "XMLHttpRequest",
            "X-CSRF-TOKEN": self.csrf or "",
            "Referer": referer or SITE_URL,
            "Origin": origin,
        }

    def get_page(self, path: str) -> str:
        """GET an HTML page (series / competition / event-details pages)."""
        parts = urlsplit(SITE_URL)
        url = f"{parts.scheme}://{parts.netloc}{path}"
        r = self.session.get(url, headers=self._headers(), timeout=45)
        return r.text

    def api_post(self, path: str, data: dict | None = None,
                 params: dict | None = None) -> dict:
        """POST to a JSON API endpoint (auto-attaches the csrf _token).

        The site ends a login session after some hours, and every POST then
        answers "CSRF token mismatch." (Laravel's 419) -- refused before the
        app runs, so nothing happened and retrying once after a fresh login
        is safe, a placeBet included.
        """
        resp = self._api_post_once(path, data, params)
        if _session_expired(resp):
            logger.info("Session expired for %s, logging in again", self.username)
            if self.login():
                resp = self._api_post_once(path, data, params)
        return resp

    def _api_post_once(self, path: str, data: dict | None,
                       params: dict | None) -> dict:
        parts = urlsplit(SITE_URL)
        url = f"{parts.scheme}://{parts.netloc}{path}"
        payload = dict(data or {})
        payload["_token"] = self.csrf or ""
        headers = self._headers()
        headers["Accept"] = "application/json"
        r = self.session.post(url, data=payload, params=params,
                              headers=headers, timeout=20)
        try:
            return r.json()
        except ValueError:
            return {"_error": True,
                    "message": f"Non-JSON ({r.status_code}): {r.text[:160]}"}

    def get_market_book(self, event_type: str, market_ids: list[str],
                        referer: str | None = None) -> dict:
        """POST the odds provider for live MarketBooks (<=5 ids per call).

        Returns the parsed {"odds": [...]} body. The provider times out
        occasionally (observed live 2026-10-10) -- two retries, since one
        dead odds fetch otherwise aborts a whole bet conversation. Raises
        on non-JSON after the retries.
        """
        q = ",".join(market_ids[:5])
        url = (f"{ODDS_HOST}/api/v1/get/getMarketBook/{event_type}"
               f"?q={q}&api_mode=1&eventType={event_type}")
        headers = {
            "Origin": SITE_URL,
            "Referer": referer or SITE_URL,
            "User-Agent": _ODDS_UA,
            "X-CSRF-TOKEN": self.csrf or "",
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "*/*",
        }
        # A 502/503/504 is the provider's own gateway timing out (an HTML
        # page, not JSON) -- as transient as a dropped connection, so it is
        # retried the same way instead of failing the conversation at once.
        last_err = ""
        attempts = 4
        for attempt in range(attempts):
            try:
                r = self.session.post(url, headers=headers, timeout=30)
            except Exception as exc:      # timeout / connection reset
                last_err = str(exc)[:120]
            else:
                if r.status_code in (502, 503, 504):
                    last_err = f"gateway error {r.status_code}"
                else:
                    try:
                        return r.json()
                    except ValueError:
                        raise RuntimeError(
                            f"odds provider sent a non-JSON reply "
                            f"(HTTP {r.status_code})")
            if attempt < attempts - 1:
                time.sleep(2 * (attempt + 1))
        raise RuntimeError(
            f"odds provider is not responding ({last_err}) -- "
            f"their server is busy, try again in a minute")

    def get_unmatched_ids(self) -> list[str]:
        """Bet ids of every currently-unmatched bet, via the site's own
        /unmatchedbetdetails panel (HTML). Extracted the same way the site's
        cancel_unmatcedbet() JS reads them: the cancel control carries
        id=<betId>, and rows carry class unmatched_bet_<betId>."""
        resp = self.api_post("/unmatchedbetdetails", {"bets_name": "unmatched"})
        if resp.get("_error"):
            raise RuntimeError(resp.get("message") or "unmatchedbetdetails failed")
        res = resp.get("res") or ""
        ids: list[str] = []
        # Primary: elements wired to the site's cancel handler.
        for m in re.finditer(r"<\w+[^>]*\bcancel_unmatcedbet\b[^>]*>", res):
            tag = m.group(0)
            im = re.search(r'\bid=["\']([^"\']+)["\']', tag)
            # edit_<id> buttons open the partial-edit UI, never cancel.
            if im and not im.group(1).startswith("edit_"):
                ids.append(im.group(1))
        # Secondary: row classes (same id the JS derives as main_id).
        ids.extend(re.findall(r"unmatched_bet_(\d+)", res))
        seen, out = set(), []
        for i in ids:
            if i not in seen:
                seen.add(i)
                out.append(i)
        return out

    def cancel_bet(self, bet_id: str) -> tuple[bool, str]:
        """Cancel one unmatched bet. Response key is statusCode (not status)."""
        resp = self.api_post("/api2/v2/cancelbet", {"id": bet_id},
                             params={"desktop": "1"})
        ok = resp.get("statusCode") == 200
        return ok, resp.get("message") or ("cancelled" if ok else str(resp)[:100])


# Global account list
accounts: list[AccountSession] = []
_sheet = None          # gspread worksheet handle, set by load_accounts_from_sheet()

def load_accounts_from_sheet() -> list[AccountSession]:
    global _sheet
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=scopes)
    client = gspread.authorize(creds)
    book = client.open_by_key(SPREADSHEET_ID)
    if WORKSHEET_GID:
        try:
            _sheet = book.get_worksheet_by_id(int(WORKSHEET_GID))
        except (ValueError, gspread.WorksheetNotFound):
            have = ", ".join(f"{w.title} (gid {w.id})" for w in book.worksheets())
            raise RuntimeError(
                f"no tab with gid {WORKSHEET_GID} in that spreadsheet. "
                f"Tabs it has: {have}. Fix EXCHANGE_WORKSHEET_GID.") from None
    elif WORKSHEET_NAME:
        try:
            _sheet = book.worksheet(WORKSHEET_NAME)
        except gspread.WorksheetNotFound:
            # Naming the tabs that DO exist, because the bare gspread error
            # is just the missing title -- and a new site instance usually
            # fails here first (its accounts tab has not been made yet).
            have = ", ".join(w.title for w in book.worksheets())
            raise RuntimeError(
                f"no tab named {WORKSHEET_NAME!r} in that spreadsheet. "
                f"Tabs it has: {have}. Make one with USERNAME | PASSWORD | "
                f"BALANCE columns, or change EXCHANGE_WORKSHEET_NAME.") from None
    else:
        _sheet = book.sheet1
    rows = _sheet.get_all_values()
    result = []
    # row numbers recorded so balances can be written back later; empty rows
    # are skipped but the real row index is kept.
    for idx, row in enumerate(rows[1:], start=2):
        if len(row) >= 2 and row[0].strip() and row[1].strip():
            result.append(AccountSession(row[0].strip(), row[1].strip(),
                                         sheet_row=idx))
    # Make sure a BALANCE column header exists (col C).
    try:
        if rows and (len(rows[0]) < 3 or not rows[0][2].strip()):
            _sheet.update_cell(1, 3, "BALANCE")
    except Exception as exc:
        logger.warning("Could not set BALANCE header: %s", exc)
    return result

def write_balances_to_sheet():
    """Push each account's last balance into column C (best-effort).

    One batch_update for all rows -- Google's per-minute write quota is the
    real ceiling, same reasoning as channel_info.py. A failure is logged,
    never raised: a sheet hiccup must not break a bet conversation.
    """
    if _sheet is None:
        return
    cells = [(a.sheet_row, a.balance) for a in accounts
             if a.sheet_row is not None and a.balance is not None]
    if not cells:
        return
    try:
        lo = min(r for r, _ in cells)
        hi = max(r for r, _ in cells)
        values = [[""] for _ in range(hi - lo + 1)]
        for r, b in cells:
            values[r - lo][0] = f"{b:.2f}"
        _sheet.batch_update([{"range": f"C{lo}:C{hi}", "values": values}])
    except Exception as exc:
        logger.warning("Could not write balances to sheet: %s", exc)

def _refresh_balances_and_write():
    """Refresh every ready account's balance, then push to the sheet."""
    _run_parallel(lambda a: a.refresh_balance(), _logged_in())
    write_balances_to_sheet()

def login_all_accounts() -> tuple[int, int]:
    ok = fail = 0
    with ThreadPoolExecutor(max_workers=BET_CONCURRENCY) as pool:
        futures = {pool.submit(a.login): a for a in accounts}
        for f in as_completed(futures):
            a = futures[f]
            try:
                if f.result():
                    ok += 1
                    logger.info("✅ %s balance=₹%s", a.username, a.balance)
                else:
                    fail += 1
                    logger.warning("❌ %s: %s", a.username, a.error)
            except Exception as e:
                fail += 1
                a.error = str(e)[:200]
    write_balances_to_sheet()
    return ok, fail


# ═══════════════════════════════════════════════════════════════════════
#  PARSERS  (pure functions, verified against saved live HTML 2026-10-09)
# ═══════════════════════════════════════════════════════════════════════

def parse_competitions(html: str) -> list[dict]:
    """Series list from /exchange/cricket-exchange.

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
    """Matches from a cricket page or a /ex/competition/{id} page.

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
    """Market structure from /ex/event-details/{eventId}.

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
        mk["runners"].setdefault(rid, html_unescape(d.get("data-runner_name") or f"Runner {rid}"))
    match_odds = next(
        (mk for mk in markets.values() if mk["market_name"] == "Match Odds"),
        None)
    return {
        "limit": lim.group(1) if lim else "",
        "match_odds": match_odds,
        "markets": list(markets.values()),
    }


def parse_odds_response(data: dict) -> dict:
    """Normalize a getMarketBook body.

    Index 0 of availableToBack/availableToLay is the site's level-0 cell
    (read from its updateData() JS), so that is what we surface as the
    best back/lay. runner.ltp and market hash are encrypted blobs the site
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


# ═══════════════════════════════════════════════════════════════════════
#  BET OPERATIONS
# ═══════════════════════════════════════════════════════════════════════

def place_bet_on_account(
    account: AccountSession, *,
    event_id: str, event_type: str, market_id: str, market_name: str,
    runner_id: str, runner_name: str, teams: str,
    odds: float, stake: float, side: str,
    matched: str = "", ltp: str = "", limit: str = "",
) -> dict:
    """Place a Back or Lay bet on one account.

    Payload mirrors the site's own betslip form serialize() exactly (field
    list and value sources confirmed from the page's inline JS 2026-10-09):
    bet_type is availableToBack/availableToLay (the ladder side),
    betting_type is Back/Lay (the slip label), profit = stake*(odds-1)
    for both sides (liability on Lay), matched = the market's hash blob,
    ltp = the runner's ltp blob (page default "na" when absent),
    expo = 0 (fresh exposure; the site's own default for a new bet).
    """
    bet_type = "availableToBack" if side == "back" else "availableToLay"
    betting_type = "Back" if side == "back" else "Lay"
    profit = stake * (odds - 1)
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
        "betting_type": betting_type,
        "profit": f"{profit:.2f}",
        "bet_type": bet_type,
    }
    resp = account.api_post("/api2/v2/placeBet", data)
    # Logged in full: the chat only shows a trimmed message, and the site's
    # generic "Something went wrong" says nothing on its own.
    logger.info("placeBet %s %s %s @%s x%s -> %s", account.username,
                betting_type, runner_name, data["odds"], data["stake"],
                str(resp)[:300])
    status = resp.get("status")
    if status == 205:
        # The site itself redirects to /logout?q=routinecheck for this.
        account.flagged = True
        return {"ok": False, "flagged": True,
                "message": "account placed under routine check (needs review)"}
    if status == 200:
        return {"ok": True, "message": resp.get("message") or "Bet placed successfully"}
    if resp.get("_error"):
        return {"ok": False, "message": resp.get("message") or "request failed"}
    return {"ok": False,
            "message": (resp.get("message") or str(resp)[:100])}


def cancel_all_on_account(account: AccountSession) -> dict:
    """Cancel every unmatched bet on one account (site has no cancel-all
    endpoint -- its own UI cancels one id at a time, we loop)."""
    ids = account.get_unmatched_ids()
    if not ids:
        return {"cancelled": 0, "errors": []}
    errors = []
    for bid in ids:
        try:
            ok, msg = account.cancel_bet(bid)
        except Exception as exc:
            ok, msg = False, str(exc)[:80]
        if not ok:
            errors.append(f"{bid}: {msg[:40]}")
    return {"cancelled": len(ids) - len(errors), "errors": errors}


# ═══════════════════════════════════════════════════════════════════════
#  TELEGRAM HANDLERS
# ═══════════════════════════════════════════════════════════════════════

ST_SELECT_SPORT, ST_SELECT_SERIES, ST_SELECT_MATCH, ST_SELECT_SIDE, ST_ENTER_BET = range(5)

# Sports whose exchange pages share cricket's layout (series -> matches ->
# a "Match Odds" market). Checked live 2026-10-15: football (eventType 1,
# 3 runners incl. The Draw) and tennis (eventType 2) parse with the same
# functions, and the site applies the same 1.01-5.0 odds rule and 2000
# minimum stake to both. Horse racing is laid out differently (no series
# links at all) and is deliberately left out.
SPORTS = {
    "cricket": ("Cricket", "🏏", "/exchange/cricket-exchange"),
    "football": ("Football", "⚽", "/exchange/football-exchange"),
    "tennis": ("Tennis", "🎾", "/exchange/tennis-exchange"),
}
SERIES_PAGE_SIZE = 30
_user_state: dict[int, dict] = {}

def _sport_emoji(chat_id: int) -> str:
    return SPORTS[(_user_state.get(chat_id) or {}).get("sport", "cricket")][1]


def _logged_in() -> list[AccountSession]:
    # flagged = placeBet answered 205 ("routine check"); the site logged the
    # session out itself, and retrying bets on it only digs the hole.
    return [a for a in accounts if a.logged_in and not a.flagged]


def _run_parallel(fn, items, workers=None):
    """Run fn(item) over items on a small pool; returns list of results in
    item order. Exceptions from fn propagate per-item as raised."""
    results = [None] * len(items)
    with ThreadPoolExecutor(max_workers=workers or BET_CONCURRENCY) as pool:
        futs = {pool.submit(fn, it): i for i, it in enumerate(items)}
        for f in as_completed(futs):
            results[futs[f]] = f.result()
    return results


# ── /start ──
@require_admin
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    n_ok = len(_logged_in())
    n_flag = sum(1 for a in accounts if a.flagged)
    flag_line = (f"\n⚠️ {n_flag} flagged (routine check) -- /relogin to retry"
                 if n_flag else "")
    await update.message.reply_text(
        f"🏏 *Exchange Bet Bot — {SITE_NAME}*\n\n"
        f"Accounts: {len(accounts)} loaded, {n_ok} ready{flag_line}\n"
        f"Proxies: {len(_proxies)}\n"
        f"Min stake: ₹{MIN_STAKE}\n\n"
        "*Commands:*\n"
        "/event — Series → match → Back/Lay → odds+amount → place on all IDs\n"
        "🧪 on the confirm screen places one unmatched test bet and cancels it\n"
        "/cancelall — Cancel all unmatched bets (all IDs)\n"
        "/refresh — Refresh balances + exposure\n"
        "/ids — List all accounts\n"
        "/relogin — Re-login all accounts\n"
        "/addadmin `<id>` / /removeadmin `<id>` — manage admins (master)\n",
        parse_mode="Markdown")

# ── /ids ──
@require_admin
async def ids_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not accounts:
        await update.message.reply_text("No accounts loaded.")
        return
    lines = [f"*Accounts ({len(accounts)}):*\n"]
    for i, a in enumerate(accounts, 1):
        if a.flagged:
            lines.append(f"{i}. ⚠️ `{a.username}` — flagged (routine check)")
        elif a.logged_in:
            bal = f"₹{a.balance:,.2f}" if a.balance is not None else "?"
            exp = f"₹{a.exposure:,.2f}" if a.exposure is not None else "₹0"
            lines.append(f"{i}. ✅ `{a.username}` — {bal} (exp: {exp})")
        else:
            lines.append(f"{i}. ❌ `{a.username}` — {(a.error or 'n/a')[:50]}")
    text = "\n".join(lines)
    for s in range(0, len(text), 4000):
        await update.message.reply_text(text[s:s+4000], parse_mode="Markdown")

# ── /refresh ──
@require_admin
async def refresh_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    active = _logged_in()
    if not active:
        await update.message.reply_text("❌ No accounts logged in. /relogin")
        return
    msg = await update.message.reply_text(f"🔄 Refreshing {len(active)} accounts…")
    try:
        await asyncio.get_running_loop().run_in_executor(
            None, _refresh_balances_and_write)
    except Exception as exc:
        await msg.edit_text(f"❌ Refresh failed: {str(exc)[:100]}")
        return
    total_bal = sum(a.balance or 0 for a in active)
    total_exp = sum(a.exposure or 0 for a in active)
    lines = [f"*Balances* ({len(active)} accounts)\n"]
    for a in active:
        bal = f"₹{a.balance:,.2f}" if a.balance is not None else "?"
        exp = f"₹{a.exposure:,.2f}" if a.exposure is not None else "₹0"
        lines.append(f"• `{a.username}`: {bal} (exp: {exp})")
    lines.append(f"\n💰 *Total:* ₹{total_bal:,.2f}  |  Exp: ₹{total_exp:,.2f}")
    await msg.edit_text("\n".join(lines), parse_mode="Markdown")

# ── /relogin ──
@require_admin
async def relogin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global accounts
    msg = await update.message.reply_text("🔄 Reloading from sheet…")
    def _do():
        # Fresh objects from the sheet also clear any 205 flags.
        new_accounts = load_accounts_from_sheet()
        return new_accounts, login_all_accounts_with(new_accounts)
    try:
        new_accounts, (ok, fail) = await asyncio.get_running_loop().run_in_executor(None, _do)
    except Exception as exc:
        await msg.edit_text(f"❌ {str(exc)[:150]}")
        return
    accounts = new_accounts
    write_balances_to_sheet()
    await msg.edit_text(f"✅ {ok} logged in, ❌ {fail} failed. /ids for details.")

def login_all_accounts_with(pool_accounts: list[AccountSession]) -> tuple[int, int]:
    ok = fail = 0
    with ThreadPoolExecutor(max_workers=BET_CONCURRENCY) as pool:
        futures = {pool.submit(a.login): a for a in pool_accounts}
        for f in as_completed(futures):
            a = futures[f]
            try:
                if f.result():
                    ok += 1
                else:
                    fail += 1
                    logger.warning("❌ %s: %s", a.username, a.error)
            except Exception as e:
                fail += 1
                a.error = str(e)[:200]
    return ok, fail


# ── /event → series ──
@require_admin
async def event_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _logged_in():
        await update.message.reply_text("❌ No accounts logged in. /relogin")
        return ConversationHandler.END
    _user_state[update.effective_chat.id] = {}
    buttons = [[InlineKeyboardButton(f"{emoji} {label}", callback_data=f"sp:{key}")]
               for key, (label, emoji, _path) in SPORTS.items()]
    buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="sp:_cancel")])
    await update.message.reply_text(
        "Select a sport:", reply_markup=InlineKeyboardMarkup(buttons))
    return ST_SELECT_SPORT


def _series_keyboard(series: list[dict], page: int) -> InlineKeyboardMarkup:
    """One page of series buttons (football lists ~150 series, more than a
    Telegram message should carry), with Prev/Next when there are more."""
    chunk = series[page * SERIES_PAGE_SIZE:(page + 1) * SERIES_PAGE_SIZE]
    buttons = [[InlineKeyboardButton(s["name"][:48], callback_data=f"sr:{s['comp_id']}")]
               for s in chunk]
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀ Prev", callback_data=f"sr:_page:{page - 1}"))
    if (page + 1) * SERIES_PAGE_SIZE < len(series):
        nav.append(InlineKeyboardButton("Next ▶", callback_data=f"sr:_page:{page + 1}"))
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="sr:_cancel")])
    return InlineKeyboardMarkup(buttons)


def _series_title(state: dict, page: int) -> str:
    series = state.get("series") or []
    pages = max(1, -(-len(series) // SERIES_PAGE_SIZE))
    label, emoji, _ = SPORTS[state.get("sport", "cricket")]
    more = f" — page {page + 1}/{pages}" if pages > 1 else ""
    return f"{emoji} *{label} Series* ({len(series)} found{more})\nSelect a series:"


# ── sport → series ──
async def _on_sport_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    key = query.data.split(":", 1)[1]
    if key == "_cancel" or key not in SPORTS:
        await query.edit_message_text("Cancelled.")
        return ConversationHandler.END

    label, emoji, path = SPORTS[key]
    active = _logged_in()
    if not active:
        await query.edit_message_text("❌ No accounts logged in. /relogin")
        return ConversationHandler.END
    await query.edit_message_text(f"🔍 Loading {label.lower()} series…")
    try:
        html = await asyncio.get_running_loop().run_in_executor(
            None, lambda: active[0].get_page(path))
        series = parse_competitions(html)
    except Exception as exc:
        await query.edit_message_text(f"❌ Could not load series: {str(exc)[:120]}")
        return ConversationHandler.END

    if not series:
        await query.edit_message_text(f"❌ No series found on the {label.lower()} exchange page.")
        return ConversationHandler.END

    state = _user_state.setdefault(update.effective_chat.id, {})
    state.update({"sport": key, "series": series})
    await query.edit_message_text(
        _series_title(state, 0), reply_markup=_series_keyboard(series, 0),
        parse_mode="Markdown")
    return ST_SELECT_SERIES


# ── series → matches ──
async def _on_series_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data.split(":", 1)[1]
    if data == "_cancel":
        await query.edit_message_text("Cancelled.")
        return ConversationHandler.END

    chat_id = update.effective_chat.id
    if data.startswith("_page:"):
        state = _user_state.get(chat_id) or {}
        page = int(data.split(":", 1)[1])
        await query.edit_message_text(
            _series_title(state, page),
            reply_markup=_series_keyboard(state.get("series") or [], page),
            parse_mode="Markdown")
        return ST_SELECT_SERIES
    _user_state.setdefault(chat_id, {})["comp_id"] = data
    await query.edit_message_text("🔍 Loading matches…")

    active = _logged_in()
    try:
        html = await asyncio.get_running_loop().run_in_executor(
            None, lambda: active[0].get_page(f"/ex/competition/{data}"))
        matches = parse_match_list(html)
    except Exception as exc:
        await query.edit_message_text(f"❌ Could not load matches: {str(exc)[:120]}")
        return ConversationHandler.END

    if not matches:
        await query.edit_message_text("❌ No matches in this series right now.")
        return ConversationHandler.END

    _user_state[chat_id]["matches"] = {m["event_id"]: m["name"] for m in matches}
    buttons = [[InlineKeyboardButton(
        m["name"][:48], callback_data=f"mt:{m['event_id']}")]
        for m in matches[:40]]
    buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="mt:_cancel")])

    await query.edit_message_text(
        f"{_sport_emoji(chat_id)} *Matches* ({len(matches)} found)\nSelect a match:",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode="Markdown")
    return ST_SELECT_MATCH


# ── match → Back/Lay sides (live odds) ──
async def _on_match_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data.split(":", 1)[1]
    if data == "_cancel":
        await query.edit_message_text("Cancelled.")
        return ConversationHandler.END

    chat_id = update.effective_chat.id
    state = _user_state.setdefault(chat_id, {})
    state["event_id"] = data
    await query.edit_message_text("🔍 Loading match odds…")

    active = _logged_in()
    acc = active[0]

    def _fetch():
        page = acc.get_page(f"/ex/event-details/{data}")
        parsed = parse_match_page(page)
        market = parsed["match_odds"]
        if not market:
            return parsed, None
        book_raw = acc.get_market_book(
            market["event_type"], [market["market_id"]],
            referer=f"{SITE_URL}/ex/event-details/{data}")
        return parsed, parse_odds_response(book_raw).get(market["market_id"])

    try:
        parsed, book = await asyncio.get_running_loop().run_in_executor(None, _fetch)
    except Exception as exc:
        await query.edit_message_text(f"❌ Could not load odds: {str(exc)[:120]}")
        return ConversationHandler.END

    market = parsed["match_odds"]
    if not market:
        await query.edit_message_text(
            "❌ No Match Odds market on this page.\n"
            "The match may not have started, or may have ended.")
        return ConversationHandler.END
    if book and book.get("status") != "OPEN":
        await query.edit_message_text(
            f"⚠️ Match Odds market is {book.get('status') or 'unknown'} right now.")
        return ConversationHandler.END

    state.update({
        "event_id": market["event_id"] or data,
        "event_type": market["event_type"],
        "market_id": market["market_id"],
        "market_name": market["market_name"],
        "teams": market["teams"],
        "limit": parsed["limit"],
        "runners": market["runners"],          # {runner_id: name}
        "book": book or {"runners": {}, "hash": "", "status": None},
    })

    buttons, odds_lines = [], []
    for rid, rname in market["runners"].items():
        rr = (book or {}).get("runners", {}).get(str(rid), {})
        back = rr.get("back")
        lay = rr.get("lay")
        back_s = f"{back:g}" if isinstance(back, (int, float)) else "—"
        lay_s = f"{lay:g}" if isinstance(lay, (int, float)) else "—"
        odds_lines.append(f"  {rname}: Back {back_s} / Lay {lay_s}")
        buttons.append([
            InlineKeyboardButton(f"⬆ BACK {rname[:16]} @{back_s}",
                                 callback_data=f"side:{rid}:back"),
            InlineKeyboardButton(f"⬇ LAY {rname[:16]} @{lay_s}",
                                 callback_data=f"side:{rid}:lay"),
        ])
    buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="side:_cancel:x")])

    match_name = _user_state.get(chat_id, {}).get("matches", {}).get(data, "")
    title = match_name or market["teams"] or f"Match {data}"
    await query.edit_message_text(
        f"{_sport_emoji(chat_id)} *{html_unescape(title)[:60]}*\nMatch Odds:\n" + "\n".join(odds_lines) +
        "\n\nPick team + Back/Lay:",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode="Markdown")
    return ST_SELECT_SIDE


# ── side → ask odds + stake ──
async def _on_side_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    parts = query.data.split(":")
    if parts[1] == "_cancel":
        await query.edit_message_text("Cancelled.")
        return ConversationHandler.END

    selection_id, side = parts[1], parts[2]
    chat_id = update.effective_chat.id
    state = _user_state.setdefault(chat_id, {})
    state["runner_id"] = selection_id
    state["side"] = side

    runners = state.get("runners", {})
    team = runners.get(selection_id) or runners.get(str(selection_id)) or "?"
    state["runner_name"] = team

    book = state.get("book") or {}
    rr = book.get("runners", {}).get(str(selection_id), {})
    cur = rr.get("back") if side == "back" else rr.get("lay")
    cur_s = f"{cur:g}" if isinstance(cur, (int, float)) else "—"

    await query.edit_message_text(
        f"{_sport_emoji(chat_id)} *{side.upper()} {html_unescape(team)}*\n"
        f"Current best: @{cur_s}\n\n"
        f"Enter odds and stake:\n"
        f"Example: `1.55 2000`\n"
        f"(stake min ₹{MIN_STAKE}; odds {MIN_ODDS}–{MAX_ODDS})\n\n"
        f"Or /cancel to abort.",
        parse_mode="Markdown")
    return ST_ENTER_BET


# ── odds + stake → confirm card ──
async def _on_bet_details(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    chat_id = update.effective_chat.id
    state = _user_state.get(chat_id)
    if not state or "runner_id" not in state:
        await update.message.reply_text("❌ No bet in progress. Start with /event.")
        return ConversationHandler.END

    parts = text.split()
    if len(parts) != 2:
        await update.message.reply_text(
            "❌ Enter odds and stake: `1.55 2000`", parse_mode="Markdown")
        return ST_ENTER_BET
    try:
        odds = float(parts[0])
        stake = float(parts[1])
    except ValueError:
        await update.message.reply_text(
            "❌ Invalid numbers. Example: `1.55 2000`", parse_mode="Markdown")
        return ST_ENTER_BET
    # Same two rules the site's own JS enforces before it will even POST.
    if odds < MIN_ODDS or odds > MAX_ODDS:
        await update.message.reply_text(
            f"❌ Odds must be {MIN_ODDS}–{MAX_ODDS} for Match Odds "
            f"(site risk-management rule).")
        return ST_ENTER_BET
    if stake < MIN_STAKE:
        await update.message.reply_text(
            f"❌ Stake amount should be minimum {MIN_STAKE} (site rule).")
        return ST_ENTER_BET

    state["odds"] = odds
    state["stake"] = stake

    active = _logged_in()
    team = html_unescape(state.get("runner_name", "?"))
    side = state.get("side", "?").upper()

    buttons = [
        [InlineKeyboardButton(f"✅ Place on {len(active)} IDs",
                              callback_data="confirm:yes")],
        [InlineKeyboardButton("🧪 Test 1 ID (unmatched, auto-cancel)",
                              callback_data="confirm:test")],
        [InlineKeyboardButton("❌ Cancel", callback_data="confirm:no")],
    ]
    await update.message.reply_text(
        f"{_sport_emoji(chat_id)} *Confirm bet:*\n\n"
        f"*{side}* {team}\n"
        f"Odds: *{odds:g}*  |  Stake: *₹{stake:,.0f}* each\n"
        f"Accounts: *{len(active)}*\n"
        f"Total stake: *₹{stake * len(active):,.0f}*\n\n"
        f"🧪 Test places an unmatched BACK @1.01 on the first ID only,\n"
        f"then cancels it — proves place→list→cancel with no exposure.",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode="Markdown")
    return ST_ENTER_BET


# ── confirm → place / test / cancel ──
async def _on_bet_confirmed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    action = query.data.split(":", 1)[1]
    chat_id = update.effective_chat.id
    state = _user_state.get(chat_id) or {}

    if action == "no":
        await query.edit_message_text("Cancelled.")
        _user_state.pop(chat_id, None)
        return ConversationHandler.END

    active = _logged_in()
    if not active or "market_id" not in state:
        await query.edit_message_text("❌ No accounts/market. /relogin or /event again.")
        _user_state.pop(chat_id, None)
        return ConversationHandler.END

    side = state.get("side", "back")
    odds = state.get("odds")
    stake = state.get("stake")

    # Re-read the book now. Its encrypted "hash" goes into placeBet as
    # `matched`, and the site's own page copies a fresh one in at click
    # time; the copy from when the match was picked can be minutes old,
    # and a stale one is refused with "Something went wrong, please try
    # refreshing the page".
    await query.edit_message_text("🔄 Refreshing odds…")
    try:
        raw = await asyncio.get_running_loop().run_in_executor(
            None, lambda: active[0].get_market_book(
                state.get("event_type", "4"), [state["market_id"]],
                referer=f"{SITE_URL}/ex/event-details/{state.get('event_id', '')}"))
        fresh = parse_odds_response(raw).get(state["market_id"])
    except Exception as exc:
        fresh = None
        logger.warning("Fresh odds fetch failed: %s", exc)
    if not fresh:
        await query.edit_message_text(
            "❌ Could not refresh the odds just before placing. "
            "Nothing was placed — try /event again.")
        _user_state.pop(chat_id, None)
        return ConversationHandler.END
    if fresh.get("status") != "OPEN":
        await query.edit_message_text(
            f"⚠️ Match Odds market is {fresh.get('status') or 'unknown'} now. "
            "Nothing was placed.")
        _user_state.pop(chat_id, None)
        return ConversationHandler.END
    state["book"] = fresh
    book = fresh
    rr = (book.get("runners") or {}).get(str(state.get("runner_id")), {})

    if action == "test":
        await _run_test_bet(query, active[0], state, book)
        _user_state.pop(chat_id, None)
        return ConversationHandler.END

    # action == "yes": place on every ready ID.
    await query.edit_message_text(
        f"⏳ {side.upper()} {html_unescape(state.get('runner_name','?'))} "
        f"@{odds:g} ₹{stake:,.0f} on {len(active)} accounts…")

    def _place(acc):
        try:
            return place_bet_on_account(
                acc,
                event_id=state.get("event_id", ""),
                event_type=state.get("event_type", "4"),
                market_id=state.get("market_id", ""),
                market_name=state.get("market_name", "Match Odds"),
                runner_id=str(state.get("runner_id", "")),
                runner_name=state.get("runner_name", ""),
                teams=state.get("teams", ""),
                odds=odds, stake=stake, side=side,
                matched=book.get("hash", ""),
                ltp=rr.get("ltp") or "na",
                limit=state.get("limit", ""),
            )
        except Exception as exc:
            return {"ok": False, "message": str(exc)[:100]}

    try:
        results = await asyncio.get_running_loop().run_in_executor(
            None, lambda: _run_parallel(_place, active))
    except Exception as exc:
        await query.edit_message_text(f"❌ Place failed: {str(exc)[:120]}")
        _user_state.pop(chat_id, None)
        return ConversationHandler.END

    ok = fail = flagged = 0
    lines = [f"*{side.upper()} {html_unescape(state.get('runner_name','?'))} "
             f"@{odds:g} ₹{stake:,.0f}*\n"]
    for acc, r in zip(active, results):
        if r.get("ok"):
            ok += 1
            lines.append(f"✅ `{acc.username}`: {r.get('message','placed')[:50]}")
        elif r.get("flagged"):
            flagged += 1
            lines.append(f"⚠️ `{acc.username}`: {r.get('message')[:60]}")
        else:
            fail += 1
            lines.append(f"❌ `{acc.username}`: {r.get('message','?')[:60]}")
    lines.append(f"\n*Result:* ✅ {ok} placed, ❌ {fail} failed"
                 + (f", ⚠️ {flagged} flagged" if flagged else ""))
    if flagged:
        lines.append("⚠️ Flagged IDs are excluded until /relogin.")

    def _ref():
        _refresh_balances_and_write()
    try:
        await asyncio.get_running_loop().run_in_executor(None, _ref)
        ready = _logged_in()
        total_bal = sum(a.balance or 0 for a in ready)
        total_exp = sum(a.exposure or 0 for a in ready)
        lines.append(f"💰 Balance: ₹{total_bal:,.2f} | Exp: ₹{total_exp:,.2f}")
    except Exception:
        pass

    await query.edit_message_text("\n".join(lines), parse_mode="Markdown")
    _user_state.pop(chat_id, None)
    return ConversationHandler.END


async def _run_test_bet(query, acc: AccountSession, state: dict, book: dict):
    """Client-approved smoke test: one unmatched BACK @1.01 on one ID,
    then cancel only the bet that appeared. Proves the full
    place -> unmatched-list -> cancel round trip with no real exposure
    (a match would require a lay priced <= 1.01, i.e. a closed market)."""
    stake = state.get("stake")
    await query.edit_message_text(
        f"🧪 Test: BACK @1.01 ₹{stake:,.0f} on `{acc.username}`…".replace("`", ""))

    try:
        before = set(acc.get_unmatched_ids())
    except Exception as exc:
        await query.edit_message_text(f"❌ Could not list unmatched bets: {str(exc)[:100]}")
        return

    res = place_bet_on_account(
        acc,
        event_id=state.get("event_id", ""),
        event_type=state.get("event_type", "4"),
        market_id=state.get("market_id", ""),
        market_name=state.get("market_name", "Match Odds"),
        runner_id=str(state.get("runner_id", "")),
        runner_name=state.get("runner_name", ""),
        teams=state.get("teams", ""),
        odds=1.01, stake=stake, side="back",
        matched=book.get("hash", ""),
        ltp=((book.get("runners") or {}).get(str(state.get("runner_id")), {}) or {}).get("ltp") or "na",
        limit=state.get("limit", ""),
    )

    lines = [f"🧪 *Test bet on* {acc.username}\n"]
    if not res.get("ok"):
        lines.append(f"{'⚠️' if res.get('flagged') else '❌'} Place: {res.get('message')}")
        await query.edit_message_text("\n".join(lines), parse_mode="Markdown")
        return
    lines.append(f"✅ Place: {res.get('message')}")

    # The panel may take a moment to list a fresh bet.
    new_ids: set[str] = set()
    for _ in range(4):
        await asyncio.sleep(1)
        try:
            new_ids = set(acc.get_unmatched_ids()) - before
        except Exception:
            continue
        if new_ids:
            break
    if not new_ids:
        lines.append("⚠️ Bet not listed as unmatched — it may have matched "
                     "(market was effectively closed) or already vanished. "
                     "Check /refresh and the site's My Bets.")
        await query.edit_message_text("\n".join(lines), parse_mode="Markdown")
        return

    cancelled, errors = 0, []
    for bid in sorted(new_ids):
        try:
            ok, msg = acc.cancel_bet(bid)
        except Exception as exc:
            ok, msg = False, str(exc)[:60]
        if ok:
            cancelled += 1
        else:
            errors.append(f"{bid}: {msg[:40]}")
    lines.append(f"🗑 Cancelled {cancelled}/{len(new_ids)} unmatched bet(s)"
                 + (f" — errors: {'; '.join(errors)}" if errors else ""))
    lines.append("\n✅ Round trip verified: place → list → cancel.")
    await query.edit_message_text("\n".join(lines), parse_mode="Markdown")


async def _cancel_conv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _user_state.pop(update.effective_chat.id, None)
    await update.message.reply_text("Cancelled.")
    return ConversationHandler.END


# ── /cancelall ──
@require_admin
async def cancelall_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    active = _logged_in()
    if not active:
        await update.message.reply_text("❌ No accounts logged in.")
        return
    msg = await update.message.reply_text(f"⏳ Cancelling on {len(active)} accounts…")

    def _one(acc):
        try:
            return acc.username, cancel_all_on_account(acc)
        except Exception as exc:
            return acc.username, {"cancelled": -1, "errors": [str(exc)[:80]]}

    try:
        results = await asyncio.get_running_loop().run_in_executor(
            None, lambda: _run_parallel(_one, active))
    except Exception as exc:
        await msg.edit_text(f"❌ {str(exc)[:120]}")
        return

    lines = ["*Cancel unmatched bets:*\n"]
    total = 0
    for u, r in results:
        n = r.get("cancelled", 0)
        errs = r.get("errors") or []
        if n > 0 and not errs:
            total += n
            lines.append(f"✅ `{u}`: cancelled {n}")
        elif n == 0 and not errs:
            lines.append(f"• `{u}`: no unmatched bets")
        elif n > 0:
            total += n
            lines.append(f"⚠️ `{u}`: cancelled {n}, errors: {'; '.join(errs)[:60]}")
        else:
            lines.append(f"❌ `{u}`: {'; '.join(errs)[:60]}")
    lines.append(f"\n🗑 Total cancelled: {total}")

    def _ref():
        _refresh_balances_and_write()
    try:
        await asyncio.get_running_loop().run_in_executor(None, _ref)
        ready = _logged_in()
        total_bal = sum(a.balance or 0 for a in ready)
        lines.append(f"💰 Total balance: ₹{total_bal:,.2f}")
    except Exception:
        pass
    await msg.edit_text("\n".join(lines), parse_mode="Markdown")


# ── /addadmin / /removeadmin (master only, persisted) ──
@require_admin
async def addadmin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_master(update.effective_user.id):
        # Say where masters come from: they are deliberately not settable
        # from inside the bot, so "Master only" alone leaves no way forward.
        await update.message.reply_text(
            "⛔ Master only. Admins can use the bot but not add others.\n"
            "A master is added by putting their ID in MASTER_ADMIN_ID in the "
            "bot's env file and restarting it.")
        return
    if not context.args:
        await update.message.reply_text("Usage: `/addadmin <id>`", parse_mode="Markdown")
        return
    for uid in context.args:
        admin_ids.add(uid.strip())
    _save_admins()
    await update.message.reply_text(f"✅ Added: {', '.join(context.args)}")


@require_admin
async def removeadmin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_master(update.effective_user.id):
        await update.message.reply_text("⛔ Master only.")
        return
    if not context.args:
        await update.message.reply_text("Usage: `/removeadmin <id>`", parse_mode="Markdown")
        return
    removed = []
    for uid in context.args:
        uid = uid.strip()
        if uid in MASTER_ADMIN_IDS:
            continue          # masters can only be changed via .env + restart
        if uid in admin_ids:
            admin_ids.discard(uid)
            removed.append(uid)
    _save_admins()
    await update.message.reply_text(
        f"✅ Removed: {', '.join(removed)}" if removed else "Nothing to remove.")


# ═══════════════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════════════

def main():
    global accounts

    if not BOT_TOKEN:
        sys.exit("ERROR: Set EXCHANGE_BOT_TOKEN")
    if not SPREADSHEET_ID:
        sys.exit("ERROR: Set EXCHANGE_SHEET_ID")
    if not MASTER_ADMIN_IDS:
        sys.exit("ERROR: Set MASTER_ADMIN_ID")

    admin_ids.update(_load_admins())

    logger.info("Loading accounts from sheet %s…", SPREADSHEET_ID)
    try:
        accounts = load_accounts_from_sheet()
    except Exception as e:
        sys.exit(f"ERROR: {e}")
    logger.info("Loaded %d accounts.", len(accounts))

    if accounts:
        logger.info("Logging in…")
        ok, fail = login_all_accounts()
        logger.info("Login: %d OK, %d failed.", ok, fail)

    app = Application.builder().token(BOT_TOKEN).build()

    event_conv = ConversationHandler(
        entry_points=[CommandHandler("event", event_cmd)],
        states={
            ST_SELECT_SPORT: [
                CallbackQueryHandler(_on_sport_selected, pattern=r"^sp:"),
            ],
            ST_SELECT_SERIES: [
                CallbackQueryHandler(_on_series_selected, pattern=r"^sr:"),
            ],
            ST_SELECT_MATCH: [
                CallbackQueryHandler(_on_match_selected, pattern=r"^mt:"),
            ],
            ST_SELECT_SIDE: [
                CallbackQueryHandler(_on_side_selected, pattern=r"^side:"),
            ],
            ST_ENTER_BET: [
                CallbackQueryHandler(_on_bet_confirmed, pattern=r"^confirm:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, _on_bet_details),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", _cancel_conv),
            CommandHandler("done", _cancel_conv),
        ],
        per_chat=True,
        # /event mid-flow restarts cleanly (state is reset in event_cmd);
        # other commands (cancelall/refresh/...) are declined by the
        # conversation and handled at app level, so they always work.
        allow_reentry=True,
    )

    app.add_handler(event_conv)
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("ids", ids_cmd))
    app.add_handler(CommandHandler("refresh", refresh_cmd))
    app.add_handler(CommandHandler("relogin", relogin_cmd))
    app.add_handler(CommandHandler("cancelall", cancelall_cmd))
    app.add_handler(CommandHandler("addadmin", addadmin_cmd))
    app.add_handler(CommandHandler("removeadmin", removeadmin_cmd))

    logger.info("Exchange bot starting  site=%s  accounts=%d  proxies=%d  min_stake=%d",
                SITE_URL, len(accounts), len(_proxies), MIN_STAKE)
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
