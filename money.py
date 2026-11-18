#!/usr/bin/env python3
"""Deposit / withdrawal / bank-account-whitelist calls, over plain HTTP.

Why its own module: main.py is already ~6k lines, and nothing in main needs
these -- the dependency only runs one way (money imports main). Same reasoning
that moved the chip solver into chip_plan.py.

Everything here was read live off cricmatch247's own inline JavaScript on
2026-11-17 with probe_deposit.py, and the paths live on the SiteProfile
(sites/base.py) rather than in this file, so the next site is profile data
rather than a fork.

The chain, and the one thing about it that is easy to get wrong
---------------------------------------------------------------
    deposit_qr()        -> a upi:// string the player pays
    request_withdraw()  -> 205 + an SMS OTP for a NEW bank account
    verify_withdraw()   -> 200, and the withdrawal order is created HERE
    cancel_withdraw()   -> 200

Verifying the OTP is what whitelists the bank account AND what creates the
order; request_withdraw() alone creates nothing. So there is nothing to
cancel until verify_withdraw() has succeeded -- cancelling after a bare 205
would be cancelling an order that does not exist.
"""
import re
import time
from urllib.parse import urljoin, urlsplit

import requests

import main as engine
from sites import profile_for

# How many auto-submitting hidden forms to follow between the site and the
# page that finally carries the UPI string. cricmatch's chain is 3 hops
# (site -> stipepay -> truelayerpayments/redirect -> .../payment); the cap is
# slack for a PSP that adds one, not a number anything depends on.
MAX_PSP_HOPS = 8

_UPI_RE = re.compile(r"upi://pay\?[^\s\"'<>\\]{10,600}")
_FORM_RE = re.compile(r"""<form[^>]*action=["']([^"']+)["'][^>]*>(.*?)</form>""",
                      re.S | re.I)
_INPUT_RE = re.compile(r"""name=["']([^"']+)["'][^>]*value=["']([^"']*)["']""")
_AUTOSUBMIT_RE = re.compile(r"\.submit\(\s*\)")


def _api_url(site_url, path):
    parts = urlsplit(site_url)
    return f"{parts.scheme}://{parts.netloc}{path}"


def _post(session, site_url, path, data, csrf, timeout=30):
    """POST one of the site's own JSON endpoints with the headers its own JS
    sends. Returns the parsed body, or a synthetic {"status": None} for a
    non-JSON reply -- which is how every caller here tells "the edge/firewall
    answered" from "the app refused me", exactly as http_login_call() does."""
    headers = {"X-Requested-With": "XMLHttpRequest", "X-CSRF-Token": csrf,
               "Referer": site_url, "Origin": engine._http_fast_origin(site_url)}
    data = dict(data, _token=csrf)
    resp = session.post(_api_url(site_url, path), data=data,
                        headers=headers, timeout=timeout)
    try:
        return resp.json()
    except ValueError:
        return {"status": None,
                "message": f"Non-JSON response (HTTP {resp.status_code}): {resp.text[:200]}"}


def player_number(session, site_url):
    """The site's own id for this account, which the deposit form submits as
    `userid`. It is printed into the logged-in page for the Pusher channel
    subscription, which is the only place it appears."""
    resp = session.get(site_url.rstrip("/") + "/", timeout=30)
    m = re.search(r"var player_number\s*=\s*'(\d+)'", resp.text)
    return m.group(1) if m else None


# --------------------------------------------------------------------------
# Deposit
# --------------------------------------------------------------------------

def deposit_qr(session, csrf, site_url, amount, userid=None):
    """Mint a payment order and return the UPI string the player pays.

    Returns {"ok", "upi", "order_id", "expiry", "amount", "messages"}.

    Two hops, not one: the site answers with an encrypted `key`, which its
    own #payNow form carries to an EXTERNAL payment gateway, and the QR
    belongs to that gateway. The chain is a series of hidden forms that a
    browser auto-submits -- re-POSTing each one does the same thing with no
    browser, which is the whole reason this is cheap.

    NOTE the order is unpaid until somebody actually pays it, and it EXPIRES
    (cricmatch's carried ~15 minutes). Mint it when you are ready to send it,
    not in a batch hours ahead.
    """
    prof = profile_for(site_url)
    out = {"ok": False, "upi": None, "order_id": None, "expiry": None,
           "amount": amount, "messages": []}
    if not prof.supports_deposit:
        out["messages"].append(f"{prof.key} has no deposit route mapped yet.")
        return out

    if userid is None:
        userid = player_number(session, site_url)
    if not userid:
        out["messages"].append("Could not read this account's player id off the page.")
        return out

    started = _post(session, site_url, prof.deposit_store_path,
                    {"hashed": "", "amount": str(amount), "coupon": "",
                     "userid": userid}, csrf)
    status = started.get("status")
    if status != 200 or not started.get("key"):
        # The site states its own minimum here (cricmatch: "Amount should not
        # be less than INR 101"), so pass the message straight through rather
        # than second-guessing it -- it is the authority on its own limits.
        out["messages"].append(started.get("message") or f"Gateway refused: {started}")
        out["infra_block"] = status is None
        return out

    url = prof.deposit_gateway_url or ""
    data = {"hashed": started["key"], "amount": str(amount), "coupon": "",
            "userid": userid}
    if not url:
        out["messages"].append(
            f"{prof.key}: deposit_gateway_url is not set, so the QR hop is unknown.")
        return out

    for hop in range(MAX_PSP_HOPS):
        try:
            resp = session.post(url, data=data, timeout=45,
                                headers={"Referer": site_url})
        except requests.RequestException as e:
            out["messages"].append(f"Payment gateway unreachable at hop {hop + 1}: {str(e)[:150]}")
            return out
        body = resp.text

        m = _UPI_RE.search(body)
        if m:
            # The string is HTML-escaped in the page it is rendered into.
            out["upi"] = m.group(0).replace("&amp;", "&")
            out["ok"] = True
            break

        form = _FORM_RE.search(body)
        if not (form and _AUTOSUBMIT_RE.search(body)):
            out["messages"].append(
                f"Gateway hop {hop + 1} ({resp.url}) carried neither a UPI string nor "
                f"another auto-submit form -- the chain changed.")
            return out
        # Carry the correlating id forward as soon as the chain reveals it.
        fields = dict(_INPUT_RE.findall(form.group(2)))
        out["order_id"] = fields.get("orderId") or out["order_id"]
        out["expiry"] = fields.get("expiry") or out["expiry"]
        url, data = urljoin(resp.url, form.group(1)), fields
    else:
        out["messages"].append(f"Gateway chain did not reach a QR in {MAX_PSP_HOPS} hops.")
        return out

    out["messages"].append(f"Pay ₹{amount} to this UPI request"
                           + (f" (order {out['order_id']})" if out["order_id"] else ""))
    return out


def qr_png(upi_string, path):
    """Render the UPI string to a PNG locally.

    Deliberately local: the gateway's own page builds its QR by handing the
    string to a third-party image service, and there is no reason to send a
    customer's payment request to anybody extra when drawing it costs nothing.
    """
    import segno  # pure python, no Pillow -- see requirements.txt
    segno.make(upi_string, error="m").save(path, scale=8, border=2)
    return path


def deposit_landed(session, csrf, site_url, since_balance, timeout_secs=900,
                   poll_secs=15):
    """Wait for a deposit to show up, by watching the balance.

    The site also pushes "Deposit successful" over a Pusher private channel,
    which would be instant rather than polled -- but that needs a websocket
    client and the channel auth endpoint, and the balance is the thing that
    actually has to move before a withdrawal can be requested. So this watches
    the figure that matters. Returns the new balance, or None on timeout.
    """
    deadline = time.time() + timeout_secs
    while time.time() < deadline:
        time.sleep(poll_secs)
        bal = engine.http_get_balance(session, csrf, site_url)
        if bal.get("status") != 200:
            continue
        b = (bal.get("balance") or {})
        raw = b.get("wallet", b.get("totalBalance", b.get("balance")))
        try:
            now = float(str(raw).replace(",", ""))
        except (TypeError, ValueError):
            continue
        if now > float(since_balance) + 0.009:
            return now
    return None


# --------------------------------------------------------------------------
# Withdrawal -- i.e. whitelisting the bank account
# --------------------------------------------------------------------------

def request_withdraw(session, csrf, site_url, amount, name, ac_number, ifsc):
    """Ask for a withdrawal to a bank account.

    Returns {"ok", "needs_otp", "order_id", "messages", "raw"}.

    `needs_otp` True means the site texted an OTP to the account's REGISTERED
    phone and is waiting for it: that is the path a NEW bank account takes,
    and verifying the code is what whitelists it. `ok` with needs_otp False
    means the account was already verified and the withdrawal went straight
    through.
    """
    prof = profile_for(site_url)
    out = {"ok": False, "needs_otp": False, "order_id": None,
           "messages": [], "raw": None}
    if not prof.supports_withdraw:
        out["messages"].append(f"{prof.key} has no withdrawal route mapped yet.")
        return out

    resp = _post(session, site_url, prof.withdraw_amount_path,
                 {"amount": str(amount), "name": name,
                  "ac_number": str(ac_number), "ifsc": ifsc}, csrf)
    out["raw"] = resp
    status = str(resp.get("status"))
    msg = resp.get("message") or resp.get("msg") or ""

    if status == "205":
        out["ok"] = True
        out["needs_otp"] = True
        # The server echoes the account number back, and verify_withdraw()
        # must send THAT one rather than what was typed -- same read-what-
        # landed rule as fill_register_form()'s phone read-back.
        out["ac_number"] = resp.get("ac_number") or ac_number
        out["messages"].append(msg or "OTP sent to the account's registered phone.")
    elif status == "200":
        out["ok"] = True
        out["order_id"] = resp.get("orderId") or resp.get("order_id")
        out["messages"].append(msg or "Withdrawal accepted (bank already verified).")
    elif status == "206":
        out["messages"].append("Account placed under routine check by the site.")
        out["routine_check"] = True
    elif status == "None":
        out["messages"].append(msg or "The edge/firewall answered, not the site.")
        out["infra_block"] = True
    else:
        out["messages"].append(msg or f"Withdrawal refused: {resp}")
    return out


def verify_withdraw(session, csrf, site_url, otp, ac_number):
    """Submit the SMS code. THIS is what whitelists the bank account and
    creates the withdrawal order.

    Returns {"ok", "order_id", "bank_id", "amount", "messages", "raw"}.
    """
    prof = profile_for(site_url)
    out = {"ok": False, "order_id": None, "bank_id": None, "amount": None,
           "messages": [], "raw": None}
    resp = _post(session, site_url, prof.withdraw_verify_otp_path,
                 {"otp": str(otp), "ac_num": str(ac_number)}, csrf)
    out["raw"] = resp
    if str(resp.get("status")) == "200":
        out["ok"] = True
        out["order_id"] = resp.get("orderId") or resp.get("order_id")
        out["bank_id"] = resp.get("id")
        out["amount"] = resp.get("withdraw_amount")
        out["messages"].append("Bank account verified; withdrawal placed.")
    else:
        out["messages"].append(resp.get("message") or resp.get("msg")
                               or f"OTP refused: {resp}")
        out["infra_block"] = resp.get("status") is None
    return out


def resend_withdraw_otp(session, csrf, site_url, phone):
    prof = profile_for(site_url)
    return _post(session, site_url, prof.withdraw_resend_otp_path,
                 {"phone": str(phone)}, csrf)


def cancel_withdraw(session, csrf, site_url, withdraw_id):
    """Cancel a pending withdrawal. 200 = cancelled.

    Verify afterwards with pending_withdrawals() rather than trusting this
    alone: a withdrawal that silently stayed live is real money leaving, and
    the same read-back rule applies here as everywhere else in this repo.
    """
    prof = profile_for(site_url)
    out = {"ok": False, "messages": [], "raw": None}
    resp = _post(session, site_url, prof.cancel_withdraw_path,
                 {"id": str(withdraw_id)}, csrf)
    out["raw"] = resp
    if str(resp.get("status")) == "200":
        out["ok"] = True
        out["messages"].append(resp.get("message") or "Withdrawal cancelled.")
    else:
        out["messages"].append(resp.get("message") or resp.get("msg")
                               or f"Cancel refused: {resp}")
        out["infra_block"] = resp.get("status") is None
    return out


def pending_withdrawals(session, csrf, site_url, days=7):
    """Withdrawal rows from the transaction history, as
    [{"id", "status", "amount", "text"}].

    The history is returned as server-rendered HTML, not JSON, so this reads
    the ids out of the markup the site's own cancel button carries
    (onclick="cancelWithdraw(this)" data-id=...). That also means the rule for
    WHEN a withdrawal may be cancelled is server-side and cannot be read here
    -- the 3-minute cooldown has to be confirmed against a real row.
    """
    prof = profile_for(site_url)
    today = time.strftime("%Y/%m/%d")
    frm = time.strftime("%Y/%m/%d", time.localtime(time.time() - days * 86400))
    resp = _post(session, site_url, prof.transaction_history_path,
                 {"page": "1", "type": "w", "from_date": frm,
                  "to_date": today, "date_fitler": ""}, csrf)

    data = resp.get("data") or {}
    html = ""
    if isinstance(data, dict):
        html = (data.get("html") or "") + (data.get("html1") or "")
    elif isinstance(data, str):
        html = data
    if not html:
        return []

    rows = []
    for m in re.finditer(r"""data-id=["'](\d+)["']""", html):
        seg = re.sub(r"<[^>]+>", " ", html[max(0, m.start() - 900):m.start() + 900])
        seg = re.sub(r"\s+", " ", seg).strip()
        rows.append({"id": m.group(1), "text": seg[:300],
                     "status": _row_status(seg)})
    # One row can carry several data-id attributes; keep the first per id.
    seen, uniq = set(), []
    for r in rows:
        if r["id"] in seen:
            continue
        seen.add(r["id"])
        uniq.append(r)
    return uniq


def _row_status(text):
    low = text.lower()
    for word in ("cancelled", "canceled", "rejected", "failed",
                 "success", "approved", "completed", "pending", "processing"):
        if word in low:
            return word
    return "unknown"


def registered_phone(session, site_url):
    """The phone number the withdrawal OTP will be texted to, or None.

    Worth reading before requesting a withdrawal: the code goes to whatever
    number is registered on the ACCOUNT, which on a bot-made account is
    whichever number the signup pool happened to use -- so the operator has
    to be holding that SIM, and the bot should say which one rather than
    leaving them to guess.

    It is read out of the withdrawal partial's own resend_withdraw_otp(),
    which hardcodes `let phone = "<number>"` per session. Established by
    differential 2026-11-18: two accounts fetched the same partial and got
    two different numbers, which is what rules out it being one of the
    site-wide support numbers that also appear in the markup.
    """
    prof = profile_for(site_url)
    if not prof.supports_withdraw:
        return None
    try:
        resp = session.get(site_url.rstrip("/") + "/append/withdrawpp", timeout=30)
    except requests.RequestException:
        return None
    m = re.search(r"resend_withdraw_otp\s*\(\s*\)\s*\{[^}]{0,200}?"
                  r"let\s+phone\s*=\s*[\"'](\d{6,15})[\"']", resp.text, re.S)
    return m.group(1) if m else None
