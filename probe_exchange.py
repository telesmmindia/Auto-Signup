#!/usr/bin/env python3
"""Probe an exchange white-label (khelstake24, starexch555): extract match data and bet-placement fields
from the actual exchange pages (they are server-rendered HTML, not JSON APIs).

Findings from probe v2:
  * Events/matches are rendered as HTML at /exchange, /exchange/cricket-exchange
  * Competition pages: /ex/competition/{id} (e.g. /ex/competition/12649673)
  * Odds: external API at odds4.starlingpay.co.uk/api/v1/get/getMarketBook/
  * Place bet: POST /api2/v2/placeBet (returned 500 with empty data = exists)
  * Cancel bet: GET /api2/v2/cancelbet?desktop=1
  * Unmatched: /unmatchedbetdetails
  * Profit: POST /api2/v2/getProfit/{type}

Default run is READ-ONLY (v3, 2026-10-09): login, fetch the logged-in event
page to capture the My-Bets / cancel / unmatched inline JS (hidden behind a
login conditional -- absent from the anonymous markup), parse the betslip
template fields (limit blob, teams, market/runner ids), and call getMarketBook
for the Match Odds market to record its live response shape. The older v2
steps that POST dummy data to placeBet / cancelbet / unmatchedbetdetails only
run with --destructive, so a normal probe run cannot touch a bet.
"""
import json
import os
import re
import sys

import requests
from dotenv import load_dotenv
load_dotenv(".env")

sys.path.insert(0, ".")
import main as engine

def _arg(flag, default):
    """--flag value, so one probe serves every site on this template."""
    if flag in sys.argv:
        i = sys.argv.index(flag)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return default

# Credentials are arguments, never literals: this repo is public (see
# CLAUDE.md, "Repo is public"). Works on any site of this white-label --
#   --url https://starexch555.com --user U --pass P [--event /ex/event-details/<id>]
SITE_URL = _arg("--url", "https://khelstake24.com").rstrip("/")
USERNAME = _arg("--user", os.environ.get("EXCHANGE_PROBE_USER", ""))
PASSWORD = _arg("--pass", os.environ.get("EXCHANGE_PROBE_PASS", ""))
if not USERNAME or not PASSWORD:
    sys.exit("Pass --user <id> --pass <pw> (or set EXCHANGE_PROBE_USER/PASS).")

# First event-details link seen on the cricket exchange page (public markup).
# Stale ids 404 once the match ends, so --event takes a current one.
DEFAULT_EVENT_PATH = _arg("--event", "/ex/event-details/36140257")


def probe_logged_in_pages(session, headers):
    """Read-only v3: capture the logged-in-only JS around the My Bets panel
    (unmatched list + cancel), the betslip form template, and the odds API."""
    print(f"{'=' * 60}\nV3 Step A: logged-in event page {DEFAULT_EVENT_PATH}\n{'=' * 60}")
    r = session.get(f"{SITE_URL}{DEFAULT_EVENT_PATH}", headers=headers, timeout=30)
    print(f"  Status: {r.status_code}  bytes: {len(r.text)}")
    html = r.text
    with open("/tmp/exchange_event_loggedin.html", "w") as f:
        f.write(html)
    print("  saved -> /tmp/exchange_event_loggedin.html")

    # The My Bets / cancel machinery is rendered only for a logged-in session.
    print("\n  -- JS mentioning cancel / unmatched / betId --")
    hits = 0
    for m in re.finditer(r"cancelbet|unmatchedbet|cancelBet|betId|bet_id", html):
        seg = html[max(0, m.start() - 400):m.start() + 500]
        seg = re.sub(r"\s+", " ", seg)
        print(f"  [{m.group(0)}] ...{seg[:700]}...")
        print("  " + "-" * 66)
        hits += 1
        if hits >= 8:
            break
    if not hits:
        print("  (none -- panel JS not on this page)")

    # Betslip form template: the `limit` blob and field order.
    print("\n  -- betslip form template inputs --")
    tmpl = re.search(r"form__'\+id\+'\".{0,6000}", html, re.S)
    i = html.find("name=\"limit\"")
    if i != -1:
        seg = html[max(0, i - 1200):i + 400]
        for inp in re.findall(r"<input[^>]*name=\"[^\"]+\"[^>]*>", seg):
            print(f"    {inp[:160]}")

    # Market/runner ids for the Match Odds market.
    cells = re.findall(r"<a[^>]*class=\"[^\"]*betslip_details[^\"]*\"([^>]*)>", html)
    mo = None
    for attrs in cells:
        d = dict(re.findall(r'(data-[a-zA-Z_]+)="([^"]*)"', attrs))
        if d.get("data-market_name") == "Match Odds":
            mo = d
            break
    if not mo:
        print("  !! no Match Odds cell found on this page")
        return html
    market_id = mo.get("data-market_type")
    event_id = mo.get("data-eventId")
    event_type = mo.get("data-eventtype", "4")
    print(f"\n  Match Odds market={market_id} event={event_id} type={event_type}")

    print(f"\n{'=' * 60}\nV3 Step B: getMarketBook (live odds shape)\n{'=' * 60}")
    url = (f"https://odds4.starlingpay.co.uk/api/v1/get/getMarketBook/"
           f"{event_type}?q={market_id}&api_mode=1&eventType={event_type}")
    print(f"  POST {url}")
    # The browser sends Origin (cross-origin CORS POST), a real UA and the
    # full-page Referer -- a bare requests call got 401 from odds4's
    # CheckDomain middleware without them (confirmed 2026-10-09).
    odds_headers = {
        "Origin": SITE_URL,
        "Referer": f"{SITE_URL}{DEFAULT_EVENT_PATH}",
        "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/129.0.0.0 Safari/537.36"),
        "X-CSRF-TOKEN": headers.get("X-CSRF-TOKEN", ""),
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "*/*",
    }
    r2 = session.post(url, headers=odds_headers, timeout=30)
    print(f"  Status: {r2.status_code}")
    try:
        data = r2.json()
        print("  Response (first 2500 chars):")
        print(json.dumps(data, indent=2)[:2500])
    except Exception:
        print(f"  Non-JSON: {r2.text[:500]}")

    # ── V3 Step C: read-only fetch of the unmatched-bets panel HTML ──
    print(f"\n{'=' * 60}\nV3 Step C: POST /unmatchedbetdetails (panel HTML)\n{'=' * 60}")
    r3 = session.post(f"{SITE_URL}/unmatchedbetdetails",
                      data={"bets_name": "unmatched", "_token": headers.get("X-CSRF-TOKEN", "")},
                      headers=headers, timeout=30)
    print(f"  Status: {r3.status_code}")
    try:
        body = r3.json()
        res = body.get("res", "")
        print(f"  res length: {len(res)}")
        print(f"  res (first 2500 chars):\n{res[:2500]}")
    except Exception:
        print(f"  Non-JSON: {r3.text[:800]}")
    return html


def main():
    # ── Login ──
    print("=" * 60)
    print("Step 1: HTTP Login")
    print("=" * 60)
    session = engine.http_session_for(None)
    csrf = engine.http_fetch_csrf(session, SITE_URL)
    resp = engine.http_login_call(session, csrf, USERNAME, PASSWORD, SITE_URL)
    print(f"  Login: {resp.get('status')} - {resp.get('message')}")
    if resp.get("status") != 200:
        print("  FAILED")
        return

    headers = {
        "X-Requested-With": "XMLHttpRequest",
        "X-CSRF-TOKEN": csrf,
        "Referer": SITE_URL,
        "Accept": "application/json, text/html, */*",
    }

    # ── v3 read-only probe (logged-in markup + live odds shape) ──
    probe_logged_in_pages(session, headers)

    # ── Fetch exchange cricket page ──
    print("\n" + "=" * 60)
    print("Step 2: Fetch /exchange/cricket-exchange")
    print("=" * 60)
    r = session.get(f"{SITE_URL}/exchange/cricket-exchange",
                    headers=headers, timeout=20)
    print(f"  Status: {r.status_code}")
    print(f"  Content-Type: {r.headers.get('content-type')}")

    html = r.text

    # Parse competition links: /ex/competition/{id}
    competitions = re.findall(
        r'href=["\'](?:https?://[^/]+)?(/ex/competition/(\d+))["\'][^>]*>([^<]+)',
        html)
    print(f"\n  Competitions found: {len(competitions)}")
    for href, comp_id, name in competitions[:10]:
        print(f"    {comp_id}: {name.strip()}")

    # Parse match links
    match_links = re.findall(
        r'href=["\'](?:https?://[^/]+)?(/ex/[^"\']+)["\']', html)
    print(f"\n  Match/event links: {len(match_links)}")
    for link in match_links[:15]:
        print(f"    {link}")

    # Parse market IDs from the page (used for odds API calls)
    market_ids = re.findall(r'data-market[_-]?id=["\']([^"\']+)["\']', html, re.I)
    if not market_ids:
        market_ids = re.findall(r'market[_-]?[Ii]d["\s:=]+["\']?(\d[\d.]+)', html)
    print(f"\n  Market IDs found: {len(market_ids)}")
    for mid in market_ids[:10]:
        print(f"    {mid}")

    # Look for data attributes on match elements
    data_attrs = re.findall(
        r'data-(event|match|market|selection|runner|competition)[^=]*=["\']([^"\']+)["\']',
        html, re.I)
    print(f"\n  Data attributes: {len(data_attrs)}")
    seen = set()
    for attr_name, attr_val in data_attrs[:30]:
        key = f"{attr_name}={attr_val}"
        if key not in seen:
            seen.add(key)
            print(f"    data-{attr_name}={attr_val}")

    # ── Fetch a specific match page ──
    if match_links:
        match_url = match_links[0]
        if not match_url.startswith("http"):
            match_url = f"{SITE_URL}{match_url}"
        print(f"\n{'='*60}")
        print(f"Step 3: Fetch match page: {match_url}")
        print(f"{'='*60}")
        r2 = session.get(match_url, headers=headers, timeout=20)
        print(f"  Status: {r2.status_code}")
        match_html = r2.text

        # Find bet-related form data
        # Look for placeBet function and its data
        placebet_match = re.search(
            r'function\s+placeBet[^{]*\{([^}]{200,2000})', match_html, re.S)
        if placebet_match:
            print(f"\n  placeBet function body (first 500 chars):")
            print(f"    {placebet_match.group(1)[:500]}")

        # Look for bet slip form fields
        form_fields = re.findall(
            r'name=["\']([^"\']+)["\']', match_html)
        print(f"\n  Form field names: {len(form_fields)}")
        for f in sorted(set(form_fields))[:30]:
            print(f"    {f}")

        # Look for data attributes on the match page
        match_data_attrs = re.findall(
            r'data-(event|match|market|selection|runner|odds|type|side|bet)[^=]*=["\']([^"\']+)["\']',
            match_html, re.I)
        print(f"\n  Match page data attributes: {len(match_data_attrs)}")
        seen2 = set()
        for attr_name, attr_val in match_data_attrs[:40]:
            key = f"{attr_name}={attr_val}"
            if key not in seen2:
                seen2.add(key)
                print(f"    data-{attr_name}={attr_val}")

        # Look for market IDs on match page
        match_market_ids = re.findall(
            r'(?:market[_-]?[Ii]d|chk_markt_id)["\s:=]+["\']?(\d[\d.]+)',
            match_html)
        print(f"\n  Market IDs on match page: {match_market_ids}")

        # Look for selection/runner IDs
        selection_ids = re.findall(
            r'(?:selection[_-]?[Ii]d|runner[_-]?[Ii]d)["\s:=]+["\']?(\d+)',
            match_html)
        print(f"  Selection IDs: {selection_ids}")

        # Look for the JavaScript that builds the placeBet POST
        ajax_calls = re.findall(
            r'(?:ajax|fetch|post)\s*\([^)]*(?:placeBet|placebet|place_bet)[^)]*\)',
            match_html, re.I | re.S)
        print(f"\n  AJAX calls to placeBet: {len(ajax_calls)}")
        for call in ajax_calls[:5]:
            print(f"    {call[:200]}")

        # Extract the full placeBet JavaScript function
        all_js = re.findall(
            r'(function\s+(?:placeBet|placebet|cancelBet|cancelbet)[^{]*\{)',
            match_html, re.I)
        print(f"\n  Bet-related functions: {all_js}")

        # Find all $.ajax or $.post calls
        ajax_posts = re.findall(
            r'\$\.(?:ajax|post)\s*\(\s*\{[^}]{0,500}\}',
            match_html, re.S)
        print(f"\n  $.ajax/$.post calls ({len(ajax_posts)}):")
        for call in ajax_posts[:10]:
            if "place" in call.lower() or "cancel" in call.lower() or "bet" in call.lower():
                print(f"    {call[:300]}")

        # Search for the exact placeBet URL reference
        placebet_urls = re.findall(
            r"""['"](/[^'"]*(?:placeBet|placebet|place_bet|cancelbet|cancel_bet)[^'"]*?)['"]""",
            match_html, re.I)
        print(f"\n  Bet-related URLs in JS: {placebet_urls}")

        # Find form data construction
        formdata_matches = re.findall(
            r'(?:FormData|formData|form_data|serialize|data\s*[=:]\s*\{)[^;]{0,500}',
            match_html, re.S)
        for fd in formdata_matches:
            if "bet" in fd.lower() or "market" in fd.lower() or "odds" in fd.lower():
                print(f"\n  Form data construction:")
                print(f"    {fd[:400]}")

    # ── Test odds API ──
    print(f"\n{'='*60}")
    print("Step 4: Test starlingpay odds API")
    print("=" * 60)

    # Use market IDs found on the page
    all_mids = market_ids or []
    if not all_mids:
        # Try from the match page
        all_mids = match_market_ids if match_links else []

    if all_mids:
        q = ",".join(all_mids[:5])
        odds_url = f"https://odds4.starlingpay.co.uk/api/v1/get/getMarketBook/4?q={q}&api_mode=1&eventType=4"
        print(f"  Fetching: {odds_url}")
        r3 = session.post(odds_url, timeout=20)
        print(f"  Status: {r3.status_code}")
        try:
            odds_data = r3.json()
            print(f"  Response (formatted, first 2000 chars):")
            print(json.dumps(odds_data, indent=2)[:2000])
        except Exception:
            print(f"  Response: {r3.text[:500]}")
    else:
        print("  No market IDs found to test with")

    # ── Steps 5-7 POST dummy/empty data to bet endpoints. They only ran to
    # ── discover the endpoints' existence and are gated behind --destructive
    # ── so a default probe run cannot touch a bet. ──
    if "--destructive" in sys.argv:
        # ── Test placeBet with deliberate bad data to see error ──
        print(f"\n{'='*60}")
        print("Step 5: Test /api2/v2/placeBet error response")
        print("=" * 60)

        # Send with missing fields to see what fields it expects
        test_data = {"_token": csrf}
        r4 = session.post(f"{SITE_URL}/api2/v2/placeBet",
                          data=test_data, headers=headers, timeout=20)
        print(f"  Empty POST: {r4.status_code}")
        print(f"  Response: {r4.text[:500]}")

        # Try with some dummy fields
        test_data2 = {
            "_token": csrf,
            "marketId": "test",
            "selectionId": "test",
            "odds": "1.5",
            "stake": "10",
            "betType": "lay",
            "side": "lay",
            "type": "lay",
        }
        r5 = session.post(f"{SITE_URL}/api2/v2/placeBet",
                          data=test_data2, headers=headers, timeout=20)
        print(f"\n  With dummy fields: {r5.status_code}")
        print(f"  Response: {r5.text[:500]}")

        # ── Test cancelbet ──
        print(f"\n{'='*60}")
        print("Step 6: Test /api2/v2/cancelbet")
        print("=" * 60)

        r6 = session.get(f"{SITE_URL}/api2/v2/cancelbet?desktop=1",
                         headers=headers, timeout=20)
        print(f"  GET cancelbet: {r6.status_code}")
        print(f"  Response: {r6.text[:500]}")

        # POST version
        r6b = session.post(f"{SITE_URL}/api2/v2/cancelbet",
                           data={"_token": csrf}, headers=headers, timeout=20)
        print(f"\n  POST cancelbet: {r6b.status_code}")
        print(f"  Response: {r6b.text[:500]}")

        # ── Test unmatchedbetdetails ──
        print(f"\n{'='*60}")
        print("Step 7: Test /unmatchedbetdetails")
        print("=" * 60)

        r7 = session.get(f"{SITE_URL}/unmatchedbetdetails",
                         headers=headers, timeout=20)
        print(f"  GET: {r7.status_code}")
        print(f"  Content-Type: {r7.headers.get('content-type')}")
        print(f"  Response: {r7.text[:500]}")

        r7b = session.post(f"{SITE_URL}/unmatchedbetdetails",
                           data={"_token": csrf}, headers=headers, timeout=20)
        print(f"\n  POST: {r7b.status_code}")
        print(f"  Response: {r7b.text[:500]}")

    # ── Test getProfit ──
    print(f"\n{'='*60}")
    print("Step 8: Test /api2/v2/getProfit/1")
    print("=" * 60)
    r8 = session.post(f"{SITE_URL}/api2/v2/getProfit/1",
                      data={"_token": csrf}, headers=headers, timeout=20)
    print(f"  Status: {r8.status_code}")
    print(f"  Response: {r8.text[:500]}")


if __name__ == "__main__":
    main()
